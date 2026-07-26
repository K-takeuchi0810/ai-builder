"""列別 (単独) 学習の重みを作り、同時学習の重みと出しぶりを比較する。

## 背景

425列の同時学習では `人気(市場)` が全重み絶対値の 23.8% を占め、次に大きい列の
18倍になった。実測 (docs/evidence/20260726_selection_effect.json、表示期間3,702レース):

    人気を選ぶと → 他に何を足しても ◎ が変わるのは 0.2〜2.2% だけ。
                   ◎が1番人気と一致 95〜96%、的中率は 31.2% で固定。
    人気を外すと → 設定ごとに ◎ は 71〜85% 変わるが、的中率は 8〜12% に落ちる。

同時学習の重みは「425列すべてを使うとき」の最適解なので、参加者が選ぶ数個の
部分集合では尺度が合わない。列別学習なら各列が単独の予測力を持つので部分集合でも
尺度が揃う。どちらを本番にするかを決めるための材料を作る。

    python scripts_fit_per_column.py --years 2021 2022 2023 2024 2025 2026
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from builder import config, matrix, memprobe, presets as ps
from builder.specs import maib_all_specs

EVID = Path(__file__).resolve().parent / "docs" / "evidence"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, required=True)
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--out", default=None, help="保存先 (既定: out/cache/preset_weights_percol.json)")
    args = ap.parse_args()

    specs = maib_all_specs()
    t = time.time()
    m = matrix.merge_matrices([matrix.build_matrix(f"{y}0101", f"{y}1231", specs)
                               for y in args.years])
    col_ids = [c["id"] for c in m["columns"]]
    print(f"[load] races={len(m['races'])} cols={len(col_ids)} "
          f"{memprobe.fmt()} {time.time()-t:.0f}s", flush=True)

    prep_all = matrix.prepare_races(m)
    prep = ps._slice_races(prep_all, config.PRESET_TRAIN_FROM, config.PRESET_TRAIN_TO)
    print(f"[train] 学習レース={len(prep)} 期間={config.PRESET_TRAIN_FROM}"
          f"〜{config.PRESET_TRAIN_TO} {memprobe.fmt()}", flush=True)

    ll_uniform = ps.log_likelihood(prep, col_ids, {c: 0.0 for c in col_ids})
    print(f"[baseline] 一様分布の平均対数尤度={ll_uniform:.4f}", flush=True)

    t = time.time()

    def progress(done, total, cid, w):
        if done % 25 == 0 or done == total:
            print(f"  [fit] {done}/{total} 列 {time.time()-t:.0f}s "
                  f"直近={cid} w={w:+.4f}", flush=True)

    weights = ps.fit_per_column(prep, col_ids, l2=args.l2, iters=args.iters,
                                progress=progress)
    print(f"[fit] 列別学習 {time.time()-t:.0f}s {memprobe.fmt()}", flush=True)

    nonzero = {k: v for k, v in weights.items() if v != 0.0}
    top = sorted(nonzero.items(), key=lambda kv: -abs(kv[1]))[:12]
    print(f"[weights] 非ゼロ={len(nonzero)}/{len(col_ids)}", flush=True)
    for k, v in top:
        print(f"          {v:+.4f}  {k}", flush=True)

    # 列別重みを全部同時に使ったときの対数尤度 (同時学習より悪いのが当然。
    # 冗長性を補正しないので全列同時投入では二重計上になる)
    ll_all = ps.log_likelihood(prep, col_ids, weights)
    print(f"[ll] 列別重みを425列同時に使った場合={ll_all:.4f} "
          f"(一様との差 {ll_all - ll_uniform:+.4f})", flush=True)

    # ★ 本題: **少数選択** のときにどちらが良いか。同時学習の重みと直接比較する。
    joint = ps.load_presets().get("weights") or {}
    print("\n[少数選択での平均対数尤度 (高いほど良い)]", flush=True)
    print(f"  {'選択':<34} {'列別':>9} {'同時':>9} {'一様':>9}", flush=True)
    rows = []
    for name, cids in _subsets(col_ids):
        lp = ps.log_likelihood(prep, cids, weights)
        lj = ps.log_likelihood(prep, cids, joint) if joint else float("nan")
        lu = ps.log_likelihood(prep, cids, {c: 0.0 for c in cids})
        rows.append({"subset": name, "columns": cids, "ll_per_column": round(lp, 4),
                     "ll_joint": round(lj, 4), "ll_uniform": round(lu, 4)})
        print(f"  {name:<34} {lp:>9.4f} {lj:>9.4f} {lu:>9.4f}", flush=True)

    out_path = Path(args.out or (Path(config.PRESET_WEIGHTS_PATH).parent
                                 / "preset_weights_percol.json"))
    result = {
        "method": "per_column_univariate",
        "train_period": [config.PRESET_TRAIN_FROM, config.PRESET_TRAIN_TO],
        "display_backtest_from": config.DISPLAY_BACKTEST_FROM,
        "n_races_trained": len(prep),
        "weights": weights,
        "column_races_passed_gate": ps.column_sample_report(prep, col_ids),
        "low_sample_columns": [
            {"column": cid, "races_passed_gate": n, "threshold": config.MIN_RACES_PER_COLUMN}
            for cid, n in sorted(ps.column_sample_report(prep, col_ids).items(),
                                 key=lambda kv: kv[1])
            if n < config.MIN_RACES_PER_COLUMN],
        "mean_log_likelihood_train": round(ll_all, 6),
        "confidence_thresholds": ps.confidence_thresholds(prep, col_ids, weights),
        "gate": {"min_horses": 4, "min_fraction": 0.5},
        "n_columns": len(col_ids),
        "columns_fingerprint": ps.columns_fingerprint(col_ids),
        "l2": args.l2,
    }
    ps.save_presets(result, out_path)
    print(f"\n列別重み: {out_path}", flush=True)

    ev = EVID / "20260726_per_column_vs_joint.json"
    ev.write_text(json.dumps({
        "years": args.years, "ll_uniform": ll_uniform,
        "ll_per_column_all_columns": ll_all,
        "popularity_weight_per_column": weights.get("popularity"),
        "popularity_weight_joint": joint.get("popularity"),
        "n_nonzero": len(nonzero),
        "subsets": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"証跡: {ev}", flush=True)
    return 0


def _subsets(col_ids: list[str]) -> list[tuple[str, list[str]]]:
    """参加者が実際に作りそうな少数選択。存在する列だけを使う。"""
    have = set(col_ids)

    def pick(*cands: str) -> list[str]:
        return [c for c in cands if c in have]

    return [
        ("人気だけ", pick("popularity")),
        ("タイム指数(同距離・直近3走)のみ", pick("agg_time_index|lb=3|m=distance")),
        ("平均着順(全レース・直近5走)のみ", pick("agg_avg_finish|lb=5|m=")),
        ("人気なし・集計3種", pick("agg_avg_finish|lb=5|m=",
                              "agg_time_index|lb=3|m=distance", "agg_margin|lb=5|m=")),
        ("人気 + 集計3種", pick("popularity", "agg_avg_finish|lb=5|m=",
                            "agg_time_index|lb=3|m=distance", "agg_margin|lb=5|m=")),
        ("コーナー系2種", pick("agg_corner_first|lb=3|m=",
                           "agg_gain_last_to_finish|lb=3|m=")),
        ("騎手・近走2種", pick("jockey_track_top3_rate", "horse_recent_90d_top3_rate")),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
