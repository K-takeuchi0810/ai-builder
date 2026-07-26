"""参加者の項目選択が **本当に印を動かすのか** を表示期間で測る。

## なぜ測るのか

425列の本番fitで `人気(市場)` の重みが +1.2037 になり、次に大きい列 (0.0666) の
18倍、中央値の 203倍を占めた。市場オッズが単独で最も強い予測子なのは事実だが、
これは製品として致命的になりうる:

  参加者が「人気」を選ぶと、他に何を選んでも ◎ が 1番人気になってしまうなら、
  「自分好みの予想AIを作る」体験は見かけだけになり、順位表も全員が
  1番人気AI と同着になる。

そこで「人気を含む設定」と「含まない設定」で、◎ が 1番人気と一致する率
(= どれだけ人気なりか) と、設定を変えたときに ◎ が変わる率を実測する。

    python scripts_check_selection_effect.py --years 2025 2026
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from builder import config, configs as cf, matrix, memprobe, model, presets as ps
from builder.specs import maib_all_specs

EVID = Path(__file__).resolve().parent / "docs" / "evidence"

# 検証する設定。「人気だけ」「人気+他」「人気なし」を対で並べ、人気の支配力を分離する。
CASES: list[tuple[str, dict]] = [
    ("人気だけ", {"step1": ["popularity"], "step2": []}),
    ("人気 + 集計3種", {"step1": ["popularity"], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 5},
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3},
        {"metric": "agg_margin", "match": [], "lookback": 5}]}),
    ("人気 + 集計3種 + 騎手/近走", {"step1": ["popularity", "jockey_track_top3_rate",
                                       "horse_recent_90d_top3_rate"], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 5},
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3},
        {"metric": "agg_margin", "match": [], "lookback": 5}]}),
    ("人気なし・集計3種", {"step1": [], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 5},
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3},
        {"metric": "agg_margin", "match": [], "lookback": 5}]}),
    ("人気なし・タイム指数のみ", {"step1": [], "step2": [
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3}]}),
    ("人気なし・コーナー系のみ", {"step1": [], "step2": [
        {"metric": "agg_corner_first", "match": [], "lookback": 3},
        {"metric": "agg_gain_last_to_finish", "match": [], "lookback": 3}]}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2025, 2026])
    ap.add_argument("--date-from", default=config.DISPLAY_BACKTEST_FROM,
                    help="表示期間の開始 (学習に使っていない期間で測る)")
    ap.add_argument("--limit-races", type=int, default=0, help="0=全件")
    ap.add_argument("--weights", default=None,
                    help="使う重みファイル (既定: 本番 preset_weights.json)")
    ap.add_argument("--tag", default="joint", help="証跡ファイル名につける識別子")
    args = ap.parse_args()

    preset = ps.load_presets(args.weights)
    if not preset.get("weights"):
        print("プリセット重みがありません")
        return 1
    specs = maib_all_specs()
    t = time.time()
    m = matrix.merge_matrices([matrix.build_matrix(f"{y}0101", f"{y}1231", specs)
                               for y in args.years])
    print(f"[load] races={len(m['races'])} {memprobe.fmt()} {time.time()-t:.0f}s", flush=True)

    races = [r for r in m["races"]
             if r["date"] >= args.date_from
             and any(h.get("order") == 1 for h in r["horses"])]
    if args.limit_races:
        races = races[: args.limit_races]
    print(f"[対象] {args.date_from} 以降の確定レース = {len(races)}", flush=True)
    if not races:
        print("対象レースがありません")
        return 1

    picks: dict[str, list[str]] = {}
    stats: dict[str, dict] = {}
    for name, cfg in CASES:
        columns = cf.selected_columns(cfg)
        weights = cf.column_weights(cfg, preset["weights"])
        n_nonzero = sum(1 for c in columns if weights.get(c["id"]))
        agree = won = show = 0
        got: list[str] = []
        for r in races:
            rows = {h["num"]: h["x"] for h in r["horses"]}
            ranked = model.score_columns_detailed(rows, columns, weights)["ranked"]
            top = ranked[0][0] if ranked else None
            got.append(top or "")
            fav = next((h["num"] for h in r["horses"] if h.get("pop") == 1), None)
            if top and fav and top == fav:
                agree += 1
            o = next((h.get("order") for h in r["horses"] if h["num"] == top), None)
            if o == 1:
                won += 1
            if isinstance(o, int) and 1 <= o <= 3:
                show += 1
        picks[name] = got
        n = len(races)
        stats[name] = {"n_columns": len(columns), "n_columns_with_weight": n_nonzero,
                       "fav_agreement": round(agree / n, 4),
                       "hit_rate_win": round(won / n, 4),
                       "hit_rate_show": round(show / n, 4)}
        s = stats[name]
        print(f"  {name:<26} 列={s['n_columns']:>2}(重み有{s['n_columns_with_weight']:>2})"
              f" ◎が1番人気と一致={s['fav_agreement']:.1%}"
              f" ◎的中={s['hit_rate_win']:.1%} ◎複勝={s['hit_rate_show']:.1%}", flush=True)

    # 設定を変えたときに ◎ が実際に変わるか (人気を含む群 / 含まない群の内部で比較)
    print("\n[設定間で ◎ が異なるレースの割合]", flush=True)
    names = [n for n, _ in CASES]
    diffs = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            d = sum(1 for x, y in zip(picks[a], picks[b]) if x != y) / len(races)
            diffs[f"{a} vs {b}"] = round(d, 4)
            print(f"  {a:<26} vs {b:<26} {d:.1%}", flush=True)

    out = EVID / f"20260726_selection_effect_{args.tag}.json"
    out.write_text(json.dumps({
        "years": args.years, "date_from": args.date_from, "n_races": len(races),
        "weights_file": args.weights or "preset_weights.json",
        "method": preset.get("method", "joint"),
        "popularity_weight": preset["weights"].get("popularity"),
        "cases": stats, "pairwise_top_pick_differs": diffs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n証跡: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
