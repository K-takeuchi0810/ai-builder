"""保存済みプリセットの自信度閾値を **尺度不変な差** で再計算する。

## なぜ必要か

当初の閾値は「1位と2位の生のスコア差」の分位点だった。生の差は選んだ項目数と
重みの大きさに比例するので、425列で決めた閾値を参加者の数項目の設定に当てると
ラベルが機能しない (実測、2026年600レース):

    同時学習の旧閾値 → 425列   : 鉄板級35% / 有力27% / 混戦38%  (ほぼ意図通り)
                       人気なし3項目: 混戦 100%
    列別学習の旧閾値 → 現実的な設定はすべて 混戦 100%

`presets.normalized_gap` はレース内の標準偏差で割るので、全重みを定数倍しても
値が変わらない。よってどの項目数の設定でも同じ閾値が使える。

閾値だけを差し替える (重みは触らない)。`scale` キーが入るので、古い形式の
閾値を掴んでいる場合は `confidence_label` が「—」を返して事故を防ぐ。

    python scripts_recompute_thresholds.py --years 2021 2022 2023 2024 2025 2026
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from builder import config, configs as cf, matrix, memprobe, model, presets as ps
from builder.specs import maib_all_specs

EVID = Path(__file__).resolve().parent / "docs" / "evidence"

# 参加者が実際に作りそうな設定。閾値を当てたときのラベル分布を確認する。
CHECK_CONFIGS: list[tuple[str, dict | None]] = [
    ("425項目すべて", None),
    ("4項目 (人気+集計3)", {"step1": ["popularity"], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 5},
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3},
        {"metric": "agg_margin", "match": [], "lookback": 5}]}),
    ("3項目 (人気なし)", {"step1": [], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 5},
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3},
        {"metric": "agg_margin", "match": [], "lookback": 5}]}),
    ("1項目 (タイム指数)", {"step1": [], "step2": [
        {"metric": "agg_time_index", "match": ["distance"], "lookback": 3}]}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, required=True)
    ap.add_argument("--files", nargs="+", default=None,
                    help="対象のプリセットファイル (既定: 本番と列別の2つ)")
    ap.add_argument("--check-year", type=int, default=None,
                    help="ラベル分布を確認する年 (既定: 最後の年)")
    args = ap.parse_args()

    cache = Path(config.PRESET_WEIGHTS_PATH).parent
    files = [Path(f) for f in (args.files or [cache / "preset_weights.json",
                                              cache / "preset_weights_percol.json"])]
    files = [f for f in files if f.exists()]
    if not files:
        print("対象のプリセットファイルがありません")
        return 1

    specs = maib_all_specs()
    t = time.time()
    m = matrix.merge_matrices([matrix.build_matrix(f"{y}0101", f"{y}1231", specs)
                               for y in args.years])
    col_ids = [c["id"] for c in m["columns"]]
    print(f"[load] races={len(m['races'])} {memprobe.fmt()} {time.time()-t:.0f}s", flush=True)

    prep_all = matrix.prepare_races(m)
    prep = ps._slice_races(prep_all, config.PRESET_TRAIN_FROM, config.PRESET_TRAIN_TO)
    print(f"[train] 閾値算出に使うレース={len(prep)} {memprobe.fmt()}", flush=True)

    check_year = args.check_year or max(args.years)
    check = [r for r in m["races"]
             if r["date"].startswith(str(check_year))
             and any(h.get("order") == 1 for h in r["horses"])][:800]
    print(f"[check] ラベル分布の確認レース={len(check)} ({check_year}年)", flush=True)

    report = {}
    for f in files:
        pr = ps.load_presets(f)
        if not pr.get("weights"):
            continue
        old = pr.get("confidence_thresholds") or {}
        t = time.time()
        new = ps.confidence_thresholds(prep, col_ids, pr["weights"])
        print(f"\n=== {f.name} ({pr.get('method', 'joint')}) ===", flush=True)
        print(f"  旧 (生の差)     鉄板級>={old.get('solid')} 有力>={old.get('strong')}")
        print(f"  新 (尺度不変)   鉄板級>={new['solid']} 有力>={new['strong']} "
              f"n={new['n']} {time.time()-t:.0f}s", flush=True)

        dist = {}
        for name, cfg in CHECK_CONFIGS:
            if cfg is None:
                cols, w = m["columns"], pr["weights"]
            else:
                cols, w = cf.selected_columns(cfg), cf.column_weights(cfg, pr["weights"])
            labs = []
            for r in check:
                rows = {h["num"]: h["x"] for h in r["horses"]}
                ranked = model.score_columns_detailed(rows, cols, w)["ranked"]
                labs.append(ps.confidence_label(
                    ps.normalized_gap([s for _n, s in ranked]), new))
            n = len(labs) or 1
            d = {k: round(labs.count(k) / n, 3) for k in ("鉄板級", "有力", "混戦", "—")}
            dist[name] = d
            print(f"    {name:<20} 鉄板級={d['鉄板級']:>5.0%} 有力={d['有力']:>5.0%} "
                  f"混戦={d['混戦']:>5.0%}" + (f" —={d['—']:.0%}" if d["—"] else ""),
                  flush=True)

        pr["confidence_thresholds"] = new
        ps.save_presets(pr, f)
        report[f.name] = {"old": old, "new": new, "label_distribution": dist}
        print(f"  保存しました: {f}", flush=True)

    ev = EVID / "20260726_confidence_rescale.json"
    ev.write_text(json.dumps({"years": args.years, "check_year": check_year,
                              "n_check_races": len(check),
                              "scale": ps.CONFIDENCE_SCALE, "files": report},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n証跡: {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
