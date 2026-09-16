"""MAIBuilder の全選択項目を未学習期間で監査する。

「項目を選ぶと印が動く」だけでは、その項目が妥当な馬を拾っている保証にならない。
本スクリプトは本番と同じ欠損ゲート・レース内正規化・プリセット重みを使い、
各列を単独選択した場合について次を測る。

- ゲート通過率（実際にその項目を使えたレースの割合）
- ◎の1着率・3着内率、勝ち馬を上位5頭に含める率
- 一様予想に対する平均対数尤度の改善
- 学習した向きを反転した方が良くないか（hold-out での符号反転検査）
- 新馬・未出走、未勝利、その他のレース区分別の同じ指標
- 値がない馬が単独項目の◎になった割合

競走条件コードは既存の read-only 索引を参照する。DB・索引・重みは変更しない。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import time

from builder import config, matrix, normalize, presets, raceclass, specs
from builder.keiba_bridge import _ensure_keiba_on_path, open_conn


def _blank() -> dict:
    return {
        "races": 0,
        "used": 0,
        "top_wins": 0,
        "top_top3": 0,
        "winner_top5": 0,
        "top_missing": 0,
        "longshot_winners": 0,
        "longshot_winner_top5": 0,
        "logp": 0.0,
        "reverse_logp": 0.0,
        "uniform_logp": 0.0,
    }


def _class_group(code: str | None) -> str:
    if code in {"701", "702"}:
        return "new"
    if code == "703":
        return "maiden"
    return "other"


def _race_key(race: dict) -> str:
    return (f"{race.get('race_year')}{race.get('race_month_day')}"
            f"{race.get('track_code')}{race.get('kaiji')}"
            f"{race.get('nichiji')}{race.get('race_num')}")


def _load_class_index() -> dict:
    path = raceclass.index_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _db_race_meta(date_from: str, date_to: str, expected: list[dict]) -> list[dict]:
    """行列と同じ list_races 順のクラス情報を返し、ずれたら停止する。"""
    _ensure_keiba_on_path()
    from scripts.backtest import horses_for_race, list_races  # type: ignore

    idx = _load_class_index()
    out = []
    with open_conn() as conn:
        raw = list_races(conn, date_from, date_to, jra_only=True, require_confirmed=True)
        for race in raw:
            horses = horses_for_race(conn, race)
            if not horses or not any(h.get("confirmed_order") == 1 for h in horses):
                continue
            got = idx.get(_race_key(race), {})
            out.append({
                "date": f"{race.get('race_year')}{race.get('race_month_day')}",
                "track": str(race.get("track_code")),
                "distance": race.get("distance"),
                "n_horses": len(horses),
                "class_code": got.get("class_code"),
                "class_group": _class_group(got.get("class_code")),
            })

    # 範囲キャッシュ作成後に同じ終了日までの結果がDBへ追記されることがある。
    # 行列は作成時点の確定レースだけを持つため、DB側の末尾が多いこと自体は正常。
    # ただし行列側の各行がDB先頭と一致することは必ず検証し、途中の欠落は許さない。
    if len(out) < len(expected):
        raise RuntimeError(f"DBより行列のレースが多い: DB={len(out)} matrix={len(expected)}")
    aligned = out[:len(expected)]
    for i, (meta, row) in enumerate(zip(aligned, expected)):
        if (meta["date"] != row.get("date")
                or meta["track"] != str((row.get("seg") or {}).get("track"))
                or meta["distance"] != (row.get("seg") or {}).get("distance")
                or meta["n_horses"] != len(row.get("horses") or [])):
            raise RuntimeError(f"DB/行列の並びが不一致: index={i} db={meta} matrix={row.get('seg')}")
    return aligned


def _softmax_logp(scores: dict[str, float], winner: str) -> float:
    values = list(scores.values())
    if not values or winner not in scores:
        return float("nan")
    high = max(values)
    denom = sum(math.exp(v - high) for v in values)
    return (scores[winner] - high) - math.log(max(denom, 1e-300))


def _add(acc: dict, *, horses: list[dict], scores: dict[str, float],
         reverse_scores: dict[str, float], winner: str, top: str,
         ranked: list[str], top_missing: bool) -> None:
    by_num = {h["num"]: h for h in horses}
    order = by_num.get(top, {}).get("order")
    winner_pop = by_num.get(winner, {}).get("pop")
    acc["used"] += 1
    acc["top_wins"] += int(order == 1)
    acc["top_top3"] += int(isinstance(order, int) and 1 <= order <= 3)
    acc["winner_top5"] += int(winner in ranked[:5])
    acc["top_missing"] += int(top_missing)
    if isinstance(winner_pop, (int, float)) and winner_pop >= 7:
        acc["longshot_winners"] += 1
        acc["longshot_winner_top5"] += int(winner in ranked[:5])
    acc["logp"] += _softmax_logp(scores, winner)
    acc["reverse_logp"] += _softmax_logp(reverse_scores, winner)
    acc["uniform_logp"] += -math.log(len(horses))


def _summary(acc: dict) -> dict:
    races, used = acc["races"], acc["used"]
    ll = acc["logp"] / used if used else None
    rev = acc["reverse_logp"] / used if used else None
    uni = acc["uniform_logp"] / used if used else None
    long_n = acc["longshot_winners"]
    return {
        "races": races,
        "used": used,
        "coverage": round(used / races, 4) if races else None,
        "top_win_rate": round(acc["top_wins"] / used, 4) if used else None,
        "top3_rate": round(acc["top_top3"] / used, 4) if used else None,
        "winner_top5_rate": round(acc["winner_top5"] / used, 4) if used else None,
        "top_missing_rate": round(acc["top_missing"] / used, 4) if used else None,
        "longshot_winners": long_n,
        "longshot_winner_top5_rate": (
            round(acc["longshot_winner_top5"] / long_n, 4) if long_n else None),
        "mean_log_likelihood": round(ll, 6) if ll is not None else None,
        "uniform_log_likelihood": round(uni, 6) if uni is not None else None,
        "log_likelihood_gain": round(ll - uni, 6) if ll is not None else None,
        "reverse_log_likelihood": round(rev, 6) if rev is not None else None,
        "reverse_minus_learned": round(rev - ll, 6) if rev is not None else None,
    }


def _status(s: dict, weight: float) -> tuple[str, list[str]]:
    reasons = []
    if weight == 0:
        reasons.append("学習重みが0")
    if (s.get("coverage") or 0) < 0.5:
        reasons.append("半数以上のレースで使用不能")
    if s.get("used", 0) < 500:
        reasons.append("検証レース500未満")
    if (s.get("log_likelihood_gain") is not None
            and s["log_likelihood_gain"] <= 0):
        reasons.append("未学習期間で一様予想以下")
    if (s.get("reverse_minus_learned") or 0) > 0.002:
        reasons.append("未学習期間では向きを反転した方が良い")
    if (s.get("top_missing_rate") or 0) > 0.02:
        reasons.append("値なし馬が単独項目の首位になる")
    if weight == 0 or s.get("used", 0) == 0:
        return "stop", reasons
    if reasons:
        return "review", reasons
    return "pass", reasons


def audit(date_from: str, date_to: str, weights_path: str | None) -> dict:
    started = time.time()
    preset = presets.load_presets(weights_path)
    all_specs = specs.maib_all_specs()
    data = matrix.build_matrix(date_from, date_to, all_specs)
    columns = data.get("columns") or []
    races = data.get("races") or []
    meta = _db_race_meta(date_from, date_to, races)
    weights = preset.get("weights") or {}

    stats = {c["id"]: {"all": _blank(), "new": _blank(),
                         "maiden": _blank(), "other": _blank()} for c in columns}
    for i, (race, rm) in enumerate(zip(races, meta), start=1):
        horses = race.get("horses") or []
        winner = next((h["num"] for h in horses if h.get("order") == 1), None)
        if not winner or len(horses) < 2:
            continue
        group = rm["class_group"]
        for c in columns:
            cid = c["id"]
            for key in ("all", group):
                stats[cid][key]["races"] += 1
            weight = float(weights.get(cid, 0.0))
            if weight == 0:
                continue
            values = {h["num"]: (h.get("x") or {}).get(cid) for h in horses}
            zs, decision, _n = normalize.race_z(values, c.get("hib", True), len(horses))
            if decision != normalize.USE:
                continue
            scores = {h["num"]: weight * zs.get(h["num"], 0.0) for h in horses}
            reverse = {num: -score for num, score in scores.items()}
            ranked = sorted(scores, key=lambda num: scores[num], reverse=True)
            top = ranked[0]
            for key in ("all", group):
                _add(stats[cid][key], horses=horses, scores=scores,
                     reverse_scores=reverse, winner=winner, top=top, ranked=ranked,
                     top_missing=values.get(top) is None)
        if i % 500 == 0:
            print(f"[audit] {i}/{len(races)} races", flush=True)

    results = []
    for c in columns:
        cid = c["id"]
        summaries = {key: _summary(value) for key, value in stats[cid].items()}
        weight = float(weights.get(cid, 0.0))
        status, reasons = _status(summaries["all"], weight)
        results.append({
            "id": cid,
            "key": c.get("key"),
            "label": c.get("label"),
            "kind": c.get("kind"),
            "higher_is_better_before_weight": bool(c.get("hib", True)),
            "weight": weight,
            "status": status,
            "reasons": reasons,
            "metrics": summaries,
        })

    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    class_counts = defaultdict(int)
    for x in meta:
        class_counts[x["class_group"]] += 1
    return {
        "date_from": date_from,
        "date_to": date_to,
        "effective_date_from": races[0].get("date") if races else None,
        "effective_date_to": races[-1].get("date") if races else None,
        "method": preset.get("method"),
        "n_races": len(races),
        "class_counts": dict(class_counts),
        "n_columns": len(columns),
        "status_counts": dict(counts),
        "elapsed_seconds": round(time.time() - started, 1),
        "thresholds": {
            "min_coverage": 0.5,
            "min_validation_races": 500,
            "reverse_advantage": 0.002,
            "max_top_missing_rate": 0.02,
        },
        "columns": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default=config.DISPLAY_BACKTEST_FROM)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--weights", default=None)
    ap.add_argument("--output", default="out/audits/feature_audit.json")
    args = ap.parse_args()
    report = audit(args.date_from, args.date_to, args.weights)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "date_from", "date_to", "n_races", "class_counts",
        "n_columns", "status_counts", "elapsed_seconds")}, ensure_ascii=False, indent=2))
    print(f"report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
