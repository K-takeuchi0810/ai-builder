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

    warnings = []
    # 判断A: v0.3 より前に保存された設定には「人気(市場)」が入っている。
    # normalize_config が落とすので印には影響しないが、黙って落とすと参加者は
    # 「選んだのに効いていない」ことに気づけないので必ず知らせる。
    dropped = cf.excluded_in_config(user_config)
    if dropped:
        warnings.append({
            "code": "excluded_columns_dropped",
            "message": "「人気(市場)」は予想に使わない項目になったため、この設定から外しました",
            "hint": "市場人気は基準の「1番人気AI」専用です。マイAIは選んだ項目だけで印を決めます。",
            "columns": [{"label": model.FEATURES[k].label} for k in dropped
                        if k in model.FEATURES],
        })

    # 選べる列が1つも無い場合。判断A で「人気(市場)」だけを選んでいた旧設定が
    # ここに落ちる (除外後に選択が空になる)。列が空だと全馬のスコアが 0 になり
    # 印は並び順そのままの無意味な値になるので、**必ず** 警告して止める。
    if not columns:
        warnings.append({
            "code": "no_columns_selected",
            "message": "予想に使える項目が選ばれていません (印は出せません)",
            "hint": "「マイAIをつくる」から項目を1つ以上選んでください。",
        })

    # プリセット重みが選択列をカバーしていないと全重み0 = 印が無意味になる。
    # 黙って順位を出すと参加者に嘘を見せるので、必ず警告として返す。
    if columns and not any(weights.get(c["id"]) for c in columns):
        warnings.append({
            "code": "no_preset_weights",
            "message": "選択された項目にプリセット重みがありません (印は無意味です)",
            "hint": "この列構成でプリセット重みを学習してください",
        })

    res = model.score_columns_detailed(rows, columns, weights)
    ranked = res["ranked"]
    used = sum(1 for c in res["columns"] if c["decision"] == "used")
    if columns and used == 0 and not warnings:
        warnings.append({
            "code": "all_columns_gated_out",
            "message": "選択された全項目がカバレッジ不足でこのレースでは使えません",
            "hint": "項目を増やすか、直近走の少ない馬が多いレースを避けてください",
        })
    _annotate_low_sample(res, preset, warnings)
    # 自信度は **尺度不変な差** で判定する。生の差は選んだ項目数と重みの大きさに
    # 比例するので、425列で決めた閾値を数項目の設定に当てると常に「混戦」になる。
    gap = ps.normalized_gap([s for _n, s in ranked])
    raw_gap = (ranked[0][1] - ranked[1][1]) if len(ranked) >= 2 else None
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
        "confidence": {"normalized_gap": None if gap is None else round(gap, 4),
                       "score_gap": None if raw_gap is None else round(raw_gap, 4),
                       "label": ps.confidence_label(gap, thresholds)},
        "weight_announced": race.get("weight_announced"),
        "config_hash": cf.config_hash(user_config),
        "warnings": warnings,
        "n_columns_used": used,
        # 予想画面を一覧レスポンスに依存させないためのヘッダ情報
        "race_num": race.get("race_num"),
        "start_time": race.get("start_time"),
        "odds_as_of": race.get("odds_as_of"),
        "odds_trusted": race.get("trusted"),
        "n_columns_selected": len(columns),
    }


def _annotate_low_sample(res: dict, preset: dict, warnings: list) -> None:
    """学習サンプルが薄い項目に印を付け、**使われている場合だけ**警告する。

    コーナー通過順位は生 JV-Data の保存範囲が 2025年以降しかなく、ゲートを通った
    学習レースが中央値 53 件しかない (賞金系は 143 件)。これらの重みは 15,000 件で
    学習した項目と同じ確度ではない。黙って同じ見た目で出すと参加者が確度を
    誤解するため、寄与の各行に印を付け、警告に項目名と学習レース数を明示する。

    **学習窓を後ろに伸ばして件数を稼ぐことはしない** (直近データで学習して直近で
    評価すれば成績が良く見えるだけで、実際の予測力ではない)。件数が薄いという
    事実をそのまま開示する。
    """
    low = {c["column"]: c["races_passed_gate"]
           for c in (preset.get("low_sample_columns") or [])}
    if not low:
        return
    for c in res["columns"]:
        if c["id"] in low:
            c["low_sample"] = True
            c["train_races"] = low[c["id"]]
    for contribs in res["contributions"].values():
        for c in contribs:
            if c["id"] in low:
                c["low_sample"] = True
                c["train_races"] = low[c["id"]]

    thin = [c for c in res["columns"] if c.get("low_sample") and c["decision"] == "used"]
    if not thin:
        return
    thin.sort(key=lambda c: c["train_races"])
    warnings.append({
        "code": "low_sample_columns",
        "message": f"選んだ項目のうち {len(thin)} 件は、重みを決めた過去レースが少ないため"
                   f"確度が低めです (最少 {thin[0]['train_races']}レース)",
        "hint": "コーナー通過順位・獲得本賞金は過去データの保存範囲が短く、"
                "他の項目より根拠が薄くなります。印はそのまま出しています。",
        "columns": [{"label": c["label"], "train_races": c["train_races"]}
                    for c in thin[:8]],
        "n_columns": len(thin),
    })


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

    warnings = []
    if acc["races"] == 0:
        # 0 レースで的中率 None を返すと「成績が悪い」と誤読されるので必ず警告する
        warnings.append({
            "code": "no_races_in_period",
            "message": f"{date_from}〜{date_to} に対象レースがありません",
            "hint": "期間を広げるか、その期間の行列を構築してください",
        })
    return {"period": [date_from, date_to],
            "note": "過去の的中率は将来の成績を保証しません",
            "config_hash": cf.config_hash(user_config),
            "warnings": warnings,
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
