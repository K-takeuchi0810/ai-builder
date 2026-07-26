"""判断B の採用手続き: 列別重みを本番にする前に採用条件3点を確認する。

判断A/B (2026-07-26 確定) の作業項目2:
    「列別fitの成果物を本番重みパスに配置、自信度閾値を新スケールで最終固定」
    「採用条件3点(列別サンプルレポート/全セル応答性/閾値再計算)の結果を証跡に保存」

同時学習で通した3条件を列別重みでもやり直す。応答性検査 (c) は重みに依存する
ので列別重みでは未実施だった。**確認せずに本番へ置かない。**

    python scripts_adopt_per_column.py --years 2021 2022 2023 2024 2025 2026
    python scripts_adopt_per_column.py --years ... --promote   # 確認後に本番配置

--promote は判定に問題が無いときだけ本番パスへ配置し、同時学習モデルを
docs/evidence/models/ へ退避する (本番経路から参照不能にする)。
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from builder import config, matrix, memprobe, presets as ps, responsiveness
from builder.specs import maib_all_specs

EVID = Path(__file__).resolve().parent / "docs" / "evidence"
MODELS = EVID / "models"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, required=True)
    ap.add_argument("--limit-races", type=int, default=400)
    ap.add_argument("--promote", action="store_true",
                    help="判定に問題が無ければ本番の重みパスへ配置する")
    args = ap.parse_args()

    cache = Path(config.PRESET_WEIGHTS_PATH)
    percol = cache.parent / "preset_weights_percol.json"
    if not percol.exists():
        print(f"列別重みがありません: {percol}")
        return 1

    pr = ps.load_presets(percol)
    specs = maib_all_specs()
    t = time.time()
    m = matrix.merge_matrices([matrix.build_matrix(f"{y}0101", f"{y}1231", specs)
                               for y in args.years])
    col_ids = [c["id"] for c in m["columns"]]
    print(f"[load] races={len(m['races'])} cols={len(col_ids)} "
          f"{memprobe.fmt()} {time.time()-t:.0f}s", flush=True)

    prep_all = matrix.prepare_races(m)
    prep = ps._slice_races(prep_all, config.PRESET_TRAIN_FROM, config.PRESET_TRAIN_TO)
    verdict: list[str] = []

    # --- 採用条件の前提: 対数尤度が一様分布以上か (列別重みは全列同時では悪化するので、
    #     全列同時ではなく **単独選択** で一様以上であることを確認する) ---
    print("\n[前提] 各列を単独で使ったときに一様分布以上か", flush=True)
    t = time.time()
    per_col, ll_uniform = ps.log_likelihood_per_column(prep, col_ids, pr["weights"])
    print(f"        一様分布の平均対数尤度={ll_uniform:.4f} ({time.time()-t:.0f}s)",
          flush=True)
    worse = [(cid, round(ll, 6), round(ll_uniform, 6))
             for cid, ll in per_col.items() if ll < ll_uniform - 1e-9]
    worse.sort(key=lambda x: x[1])
    n_better = sum(1 for ll in per_col.values() if ll > ll_uniform + 1e-9)
    print(f"        一様分布より良い列: {n_better}/{len(col_ids)}", flush=True)
    print(f"        一様分布より悪い列: {len(worse)}/{len(col_ids)}", flush=True)
    for cid, a, b in worse[:5]:
        print(f"          {cid} 列別={a} 一様={b}")
    best = sorted(per_col.items(), key=lambda kv: -kv[1])[:5]
    for cid, ll in best:
        print(f"          (最良) {cid} {ll:.4f}")
    if worse:
        verdict.append(f"単独選択で一様分布より悪い列が {len(worse)} 件")

    # --- (b) 列別有効サンプルレポート ---
    rep = pr.get("column_races_passed_gate") or ps.column_sample_report(prep, col_ids)
    low = [c for c, n in rep.items() if n < config.MIN_RACES_PER_COLUMN]
    zero = [c for c, n in rep.items() if n == 0]
    print(f"\n[b/samples] 閾値={config.MIN_RACES_PER_COLUMN} 未満: {len(low)}/{len(col_ids)}"
          f"  ゲート通過0: {len(zero)}", flush=True)
    groups: dict[str, list[int]] = {}
    for cid, n in rep.items():
        kind = ("corner" if ("corner" in cid or "gain" in cid)
                else "prize" if "prize" in cid else "other")
        groups.setdefault(kind, []).append(n)
    for k, v in sorted(groups.items()):
        v.sort()
        print(f"            {k:<7} 列数={len(v)} 中央値={v[len(v)//2]} "
              f"最小={v[0]} 最大={v[-1]}")

    # --- (c) 全セル単独選択の応答性検査 (列別重みでやり直す) ---
    window = config.DISPLAY_BACKTEST_FROM
    if not any(r["date"] >= window for r in prep_all):
        window = None
    t = time.time()
    resp = responsiveness.check_all_cells(m, pr, limit_races=args.limit_races,
                                          date_from=window)
    print(f"\n[c/responsiveness] {time.time()-t:.0f}s 評価レース={resp['n_races_evaluated']}"
          f" 窓={window or '全期間'}", flush=True)
    collapse_rate = None
    if resp.get("error"):
        print(f"            ⚠ 検査不能: {resp['message']}")
        verdict.append(f"応答性が検査不能 ({resp['error']})")
    else:
        print(f"            応答した列={resp['n_responsive']}/{resp['n_cells']} "
              f"(dead_rate={resp['dead_rate']})")
        print(f"            内訳={resp['dead_reasons']}")
        n_w = sum(1 for c in resp["cells"] if c["weight"] != 0.0)
        collapsed = resp["dead_reasons"].get("coefficient_collapsed", 0)
        collapse_rate = (collapsed / n_w) if n_w else None
        if collapse_rate is not None:
            print(f"            重み付き列={n_w} / 係数潰れ={collapsed} "
                  f"({collapse_rate:.1%})")
            if collapse_rate > 0.3:
                verdict.append(f"係数潰れ {collapse_rate:.0%} → 共線性")

    # --- (d) 自信度閾値が新スケールで入っているか ---
    th = pr.get("confidence_thresholds") or {}
    print(f"\n[d/confidence] 鉄板級>={th.get('solid')} 有力>={th.get('strong')} "
          f"scale={th.get('scale')} n={th.get('n')}", flush=True)
    if th.get("scale") != ps.CONFIDENCE_SCALE:
        verdict.append(f"自信度閾値が新スケールでない (scale={th.get('scale')})")
    if th.get("solid") is None:
        verdict.append("自信度閾値が算出できていない")

    # --- 列構成指紋 ---
    fp_ok = pr.get("columns_fingerprint") == ps.columns_fingerprint(col_ids)
    print(f"\n[指紋] {pr.get('columns_fingerprint')} 一致={fp_ok}", flush=True)
    if not fp_ok:
        verdict.append("列構成指紋が現行 spec と一致しない")

    print()
    print("[判定] " + ("; ".join(verdict) if verdict else "問題なし → 本番採用可"))

    out = EVID / "20260726_adopt_per_column.json"
    out.write_text(json.dumps({
        "years": args.years, "method": pr.get("method"),
        "n_columns": len(col_ids),
        "ll_uniform": ll_uniform,
        "n_columns_better_than_uniform_alone": n_better,
        "columns_worse_than_uniform_alone": worse[:40],
        "n_columns_worse_than_uniform_alone": len(worse),
        "low_sample_columns": len(low), "zero_gate_columns": len(zero),
        "sample_report_by_kind": {k: {"n": len(v), "median": v[len(v) // 2],
                                      "min": v[0], "max": v[-1]}
                                  for k, v in groups.items()},
        "responsiveness": {k: v for k, v in resp.items() if k != "cells"},
        "collinearity_collapse_rate": collapse_rate,
        "confidence_thresholds": th,
        "columns_fingerprint_matches_spec": fp_ok,
        "verdict": verdict or ["ok"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"証跡: {out}")

    if args.promote and not verdict:
        MODELS.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            old = ps.load_presets(cache)
            archive = MODELS / "20260726_preset_weights_joint425_NOT_FOR_PRODUCTION.json"
            payload = dict(old)
            payload["archived_on"] = "2026-07-26"
            payload["archived_reason"] = (
                "判断B で不採用。425列を同時に学習した係数は全列同時のみ整合的で、"
                "一部の単独選択では一様分布より悪化する (平均着順のみ: -2.6074 < 一様 -2.5995)。"
                "人気(市場)に重み全体の23.8%が集中し、項目追加で◎が変わるのは0.2〜2.2%だった。"
                "本番経路からは参照しない (out/cache の外に置く)。"
                "詳細: docs/evidence/20260726_FINDINGS_preset_weights.md")
            archive.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            print(f"同時学習モデルを退避: {archive}")
            cache.unlink()
        shutil.copyfile(percol, cache)
        print(f"本番の重みとして配置: {cache}")
    elif args.promote:
        print("判定に問題があるため配置しませんでした")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
