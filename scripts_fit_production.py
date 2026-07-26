"""425列モデルの本番学習と、採用前の必須検証 (a)(b)(c)(d) を一括で行う。

レビュー指示に基づく採用条件:
  (a) 本番学習 (conditional logit、単調改善保証つき)
  (b) 列別有効サンプルレポート — ゲート通過レース数が閾値未満の列を洗い出す
  (c) 全セル単独選択の応答性検査 — 共線性で係数が潰れた列を検出
  (d) 自信度閾値が当該列構成で再計算されていることの確認

使い方:
    python scripts_fit_production.py --years 2021            # パイプライン検証
    python scripts_fit_production.py --years 2021 2022 ... 2026 --save   # 本番採用
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from builder import config, matrix, presets, responsiveness
from builder.specs import maib_all_specs

EVID = Path(__file__).resolve().parent / "docs" / "evidence"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, required=True)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--lr", type=float, default=0.1)
    ap.add_argument("--limit-races", type=int, default=400,
                    help="応答性検査で評価するレース数")
    ap.add_argument("--save", action="store_true", help="本番のプリセット重みとして保存")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    specs = maib_all_specs()
    t = time.time()
    m = matrix.load_years(args.years, specs)
    col_ids = [c["id"] for c in m["columns"]]
    print(f"[load] races={len(m['races'])} columns={len(col_ids)} {time.time()-t:.0f}s",
          flush=True)

    # 一様分布 (全重み0) の基準値 — これを下回ったら採用不可
    prep_all = matrix.prepare_races(m)
    tr = [r for r in prep_all
          if config.PRESET_TRAIN_FROM <= r["date"] <= config.PRESET_TRAIN_TO]
    ll_uniform = presets.log_likelihood(tr, col_ids, {c: 0.0 for c in col_ids})
    print(f"[baseline] 一様分布の平均対数尤度 = {ll_uniform:.4f}", flush=True)

    # (a) 本番学習
    t = time.time()
    res = presets.fit_presets(m, iters=args.iters, lr=args.lr)
    ll = res["mean_log_likelihood_train"]
    print(f"[a/fit] {time.time()-t:.0f}s  平均対数尤度={ll:.4f} "
          f"({'改善' if ll > ll_uniform else '悪化'} {ll - ll_uniform:+.4f})", flush=True)
    print(f"        学習レース数={res['n_races_trained']} 期間={res['train_period']}")
    print(f"        列構成指紋={res['columns_fingerprint']} ({res['n_columns']}列)")

    # (b) 列別有効サンプルレポート
    rep = res["column_races_passed_gate"]
    low = res["low_sample_columns"]
    zero = [c for c, n in rep.items() if n == 0]
    print(f"[b/samples] 閾値={config.MIN_RACES_PER_COLUMN} 未満の列: {len(low)}/{len(col_ids)}",
          flush=True)
    print(f"            ゲート通過0レースの列: {len(zero)}")
    groups = {}
    for cid, n in rep.items():
        kind = ("corner" if ("corner" in cid or "gain" in cid)
                else "prize" if "prize" in cid else "other")
        g = groups.setdefault(kind, [])
        g.append(n)
    for k, v in sorted(groups.items()):
        v.sort()
        print(f"            {k:<7} 列数={len(v)} 中央値={v[len(v)//2]} 最小={v[0]} 最大={v[-1]}")

    # (c) 応答性検査
    t = time.time()
    resp = responsiveness.check_all_cells(m, res, limit_races=args.limit_races,
                                          date_from=config.DISPLAY_BACKTEST_FROM)
    print(f"[c/responsiveness] {time.time()-t:.0f}s  評価レース={resp['n_races_evaluated']}",
          flush=True)
    print(f"            応答した列={resp['n_responsive']}/{resp['n_cells']} "
          f"(dead_rate={resp['dead_rate']})")
    print(f"            内訳={resp['dead_reasons']}")

    # (d) 自信度閾値
    th = res["confidence_thresholds"]
    print(f"[d/confidence] 鉄板級>={th['solid']} 有力>={th['strong']} (n={th['n']})",
          flush=True)

    verdict = []
    if ll <= ll_uniform:
        verdict.append("対数尤度が一様分布以下 → 採用不可")
    if resp["dead_rate"] and resp["dead_rate"] > 0.5:
        verdict.append(f"応答しない列が過半 ({resp['dead_rate']:.0%}) → 項目別小分け学習を検討")
    if th["solid"] is None:
        verdict.append("自信度閾値が算出できていない")
    print()
    print("[判定] " + ("; ".join(verdict) if verdict else "3点すべて問題なし → 採用可"))

    tag = args.tag or f"{min(args.years)}-{max(args.years)}"
    out = EVID / f"20260726_production_fit_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "years": args.years, "n_races": len(m["races"]), "n_columns": len(col_ids),
        "ll_uniform": ll_uniform, "fit": {k: v for k, v in res.items() if k != "weights"},
        "responsiveness": {k: v for k, v in resp.items() if k != "cells"},
        "dead_cells_sample": [c for c in resp["cells"] if not c["responsive"]][:40],
        "verdict": verdict or ["ok"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"証跡: {out}")

    if args.save and not verdict:
        p = presets.save_presets(res)
        print(f"本番プリセット重みとして保存: {p}")
    elif args.save:
        print("判定に問題があるため保存しませんでした")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
