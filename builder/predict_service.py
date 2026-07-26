"""設計書 §6/§7 の中核: 印の生成 (寄与分解つき) とバックテスト再生。

API 層 (builder/api.py) はここを呼ぶだけにして、ロジックをテスト可能に保つ。

- 印: スコア降順に ◎ ○ ▲ △ × (6頭目以降は無印)
- 自信度: 1位と2位のスコア差を、学習期間の分位点で3段階 (鉄板級/有力/混戦)
- 寄与分解とカバレッジを必ず同梱 (説明可能性が本体)
- **回収率はメイン指標にしない** (設計書 §2)。バックテストは的中率系を返す。
"""

from __future__ import annotations

from . import configs as cf
from . import config as cfgmod
from . import matrix as mx
from . import model
from . import presets as ps

MARKS = ["◎", "○", "▲", "△", "×"]


def _rows_from_race(race: dict) -> dict[str, dict]:
    return {h["num"]: h["x"] for h in race.get("horses", [])}


def predict_race(race: dict, user_config: dict, preset: dict) -> dict:
    """1 レースを参加者の設定で予想する。

    race: matrix_daily / matrix の 1 レース分。preset: presets.fit_presets の結果。
    """
    columns = cf.selected_columns(user_config)
    weights = cf.column_weights(user_config, preset.get("weights") or {})
    rows = _rows_from_race(race)
    if not rows:
        return {"race_id": race.get("race_id"), "marks": [], "error": "no_horses"}

    res = model.score_columns_detailed(rows, columns, weights)
    ranked = res["ranked"]
    gap = (ranked[0][1] - ranked[1][1]) if len(ranked) >= 2 else None
    thresholds = preset.get("confidence_thresholds") or {}

    by_num = {h["num"]: h for h in race["horses"]}
    marks = []
    for i, (num, score) in enumerate(ranked):
        h = by_num.get(num, {})
        cov = res["coverage"].get(num, {})
        marks.append({
            "rank": i + 1,
            "mark": MARKS[i] if i < len(MARKS) else "",
            "horse_num": num,
            "horse_name": h.get("name"),
            "score": round(score, 4),
            "popularity": h.get("pop"),
            "odds": h.get("odds"),
            "n_past_runs": h.get("n_past_runs"),
            "coverage": cov,
            "contributions": res["contributions"].get(num, []),
        })
    return {
        "race_id": race.get("race_id"),
        "race_name": race.get("race_name"),
        "date": race.get("date"),
        "marks": marks,
        "columns": res["columns"],
        "confidence": {"score_gap": None if gap is None else round(gap, 4),
                       "label": ps.confidence_label(gap, thresholds)},
        "weight_announced": race.get("weight_announced"),
        "config_hash": cf.config_hash(user_config),
    }


def _blank():
    return {"races": 0, "win": 0, "show": 0, "in_marks": 0, "rank_corr_sum": 0.0,
            "rank_corr_n": 0}


def _spearman(pairs: list[tuple[int, int]]) -> float | None:
    """印順位と着順の Spearman 相関 (同順位は無視した簡易版)。"""
    n = len(pairs)
    if n < 2:
        return None
    d2 = sum((a - b) ** 2 for a, b in pairs)
    denom = n * (n * n - 1)
    return 1.0 - (6.0 * d2 / denom) if denom else None


def backtest(matrix: dict, user_config: dict, preset: dict, *,
             date_from: str | None = None, date_to: str = "99999999") -> dict:
    """設計書 §7 バックテスト再生。既定期間は学習に使っていない表示期間。

    返すのは的中率系 (◎単勝的中率 / ◎複勝率 / 印内的中率 / 順位相関) と
    1番人気ベースライン。回収率はメイン指標にしない。
    """
    date_from = date_from or cfgmod.DISPLAY_BACKTEST_FROM
    columns = cf.selected_columns(user_config)
    weights = cf.column_weights(user_config, preset.get("weights") or {})

    acc, base = _blank(), _blank()
    for race in matrix.get("races", []):
        if not (date_from <= race["date"] <= date_to):
            continue
        rows = _rows_from_race(race)
        if len(rows) < 2:
            continue
        order = {h["num"]: h.get("order") for h in race["horses"]}
        if not any(o == 1 for o in order.values()):
            continue                              # 結果未確定は除外

        ranked = model.score_columns_detailed(rows, columns, weights)["ranked"]
        picks = [num for num, _ in ranked]
        _tally(acc, picks, order)

        fav = next((h["num"] for h in race["horses"] if h.get("pop") == 1), None)
        if fav:
            fav_order = sorted(race["horses"],
                               key=lambda h: (h.get("pop") is None, h.get("pop") or 99))
            _tally(base, [h["num"] for h in fav_order], order)

    return {"period": [date_from, date_to],
            "note": "過去の的中率は将来の成績を保証しません",
            "config_hash": cf.config_hash(user_config),
            "your_ai": _summarize(acc), "baseline_favorite": _summarize(base)}


def _tally(acc: dict, picks: list[str], order: dict) -> None:
    acc["races"] += 1
    top = picks[0]
    o_top = order.get(top)
    if o_top == 1:
        acc["win"] += 1
    if isinstance(o_top, int) and 1 <= o_top <= 3:
        acc["show"] += 1
    winner = next((n for n, o in order.items() if o == 1), None)
    if winner in picks[:len(MARKS)]:
        acc["in_marks"] += 1
    pairs = [(i + 1, order[n]) for i, n in enumerate(picks)
             if isinstance(order.get(n), int) and order[n] > 0]
    rc = _spearman(pairs)
    if rc is not None:
        acc["rank_corr_sum"] += rc
        acc["rank_corr_n"] += 1


def _summarize(a: dict) -> dict:
    n = a["races"]
    return {
        "races": n,
        "hit_rate_win": round(a["win"] / n, 4) if n else None,
        "hit_rate_show": round(a["show"] / n, 4) if n else None,
        "hit_rate_in_marks": round(a["in_marks"] / n, 4) if n else None,
        "rank_corr": (round(a["rank_corr_sum"] / a["rank_corr_n"], 4)
                      if a["rank_corr_n"] else None),
    }
