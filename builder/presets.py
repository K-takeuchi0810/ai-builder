"""プリセット重み (設計書 方式B) の学習と、列の有効サンプル数レポート。

## 学習窓をカレンダーで切らない理由

生 JV-Data のカバーは 2025 年以降なので、素朴には「コーナー・賞金系の係数がゼロに潰れないよう
学習窓をカレンダーで制限する」ことを考える。しかし **欠損ポリシー (builder/normalize.py) の
カバレッジゲートを学習経路にも通せば、窓を切る必要は無くなる**:

    ゲートを通らない列は、そのレースで z が生成されない → 勾配に寄与しない
    → 2021 年のレースではコーナー列が単に不使用になるだけで、
      係数は「充填が閾値を満たすレース」からのみ学習される。

窓の管理をカレンダーからゲートに委譲する形。カレンダー境界の恣意性が消え、
train-serve のゲート挙動も定義上一致する。

## train-serve 一貫性 (最重要)

学習は `matrix.prepare_races()` の出力を使う。これは `normalize.race_z()` を呼ぶので、
**サービング (`model.score_race_detailed`) と同一の欠損ポリシー実装**を通る。
学習時とサービング時でゲート挙動がずれるのが最悪のスキューなので、
`tests/test_presets.py` がこれをテストで固定する (3経路一致テストの4経路目)。

## 学習方式

conditional logit (= レース内 softmax、勝ち馬を選ぶ確率の最大化)。
    score_i = Σ_k w_k z_ik ,  P(i) = softmax_race(score) ,  最大化 Σ_races log P(winner)
勾配は `z_winner - Σ_i P_i z_i` の総和。L2 正則化つき。
線形なので寄与分解が weight × z に厳密分解でき、設計書の説明可能性要件を満たす。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from . import config, matrix as mx
from . import normalize as nrm


def _slice_races(prep_races: list[dict], lo: str, hi: str) -> list[dict]:
    return [r for r in prep_races if lo <= r["date"] <= hi]


def column_sample_report(prep_races: list[dict], col_ids: list[str]) -> dict[str, int]:
    """列ごとの **ゲート通過レース数**。カレンダー充填率の代わりの検査指標。

    ゲートを通らなかった列はそのレースで z を持たないので、
    「z を持つ馬が1頭以上いるレース」を数えれば通過レース数になる。
    """
    counts = {cid: 0 for cid in col_ids}
    for r in prep_races:
        seen = set()
        for zc in r["z"].values():
            seen.update(zc.keys())
        for cid in seen:
            if cid in counts:
                counts[cid] += 1
    return counts


def gate_check(prep_races: list[dict], col_ids: list[str]) -> list[dict]:
    """デモ当日の事前検査: レースごとに「ゲートを通らなかった列」を列挙する。

    当日朝のバッチで呼び、通らないレースがあれば進行台本側で回避できるようにする。
    """
    out = []
    for i, r in enumerate(prep_races):
        seen = set()
        for zc in r["z"].values():
            seen.update(zc.keys())
        missing = [cid for cid in col_ids if cid not in seen]
        if missing:
            out.append({"index": r.get("index", i), "race_id": r.get("race_id"),
                        "date": r["date"], "missing_columns": missing,
                        "n_missing": len(missing)})
    return out


def _race_arrays(prep_races: list[dict], col_ids: list[str]):
    """(Z, winner_index) を race ごとに yield。z が無い列は 0 (= 中立)。"""
    idx = {cid: j for j, cid in enumerate(col_ids)}
    for r in prep_races:
        nums = list(r["z"].keys())
        if len(nums) < 2:
            continue
        win = next((i for i, n in enumerate(nums) if r["order"].get(n) == 1), None)
        if win is None:
            continue
        Z = np.zeros((len(nums), len(col_ids)), dtype=np.float64)
        for i, n in enumerate(nums):
            for cid, v in r["z"][n].items():
                j = idx.get(cid)
                if j is not None:
                    Z[i, j] = v
        yield Z, win


def fit_conditional_logit(prep_races: list[dict], col_ids: list[str], *,
                          l2: float = 1.0, iters: int = 200, lr: float = 0.1) -> dict:
    """conditional logit を勾配上昇で学習し {col_id: weight} を返す。

    ゲートを通らなかった列は z が全て 0 なので勾配も 0 → 重みは初期値 0 のまま。
    (= カレンダーで窓を切らずに「充填レースからのみ学習」が自動的に成立する)
    """
    races = list(_race_arrays(prep_races, col_ids))
    w = np.zeros(len(col_ids), dtype=np.float64)
    if not races:
        return {cid: 0.0 for cid in col_ids}
    n = len(races)
    for _ in range(iters):
        grad = np.zeros_like(w)
        for Z, win in races:
            s = Z @ w
            s -= s.max()
            p = np.exp(s)
            p /= p.sum()
            grad += Z[win] - (p @ Z)
        grad = grad / n - l2 * w / n
        w += lr * grad
    return {cid: float(round(w[j], 6)) for j, cid in enumerate(col_ids)}


def log_likelihood(prep_races: list[dict], col_ids: list[str],
                   weights: dict[str, float]) -> float:
    """平均対数尤度 (勝ち馬を当てる確率の対数の平均)。学習/検証の比較用。"""
    w = np.array([weights.get(cid, 0.0) for cid in col_ids], dtype=np.float64)
    total = 0.0
    cnt = 0
    for Z, win in _race_arrays(prep_races, col_ids):
        s = Z @ w
        s -= s.max()
        p = np.exp(s)
        p /= p.sum()
        total += math.log(max(p[win], 1e-12))
        cnt += 1
    return total / cnt if cnt else float("nan")


def fit_presets(matrix: dict, *, train_from: str | None = None, train_to: str | None = None,
                min_races_per_column: int | None = None,
                l2: float = 1.0, iters: int = 200, lr: float = 0.1) -> dict:
    """行列からプリセット重みを学習し、列の有効サンプル数と警告を添えて返す。

    **学習は matrix.prepare_races() 経由 = サービングと同一の欠損ポリシー実装を通る。**
    """
    train_from = train_from or config.PRESET_TRAIN_FROM
    train_to = train_to or config.PRESET_TRAIN_TO
    threshold = (min_races_per_column if min_races_per_column is not None
                 else config.MIN_RACES_PER_COLUMN)

    col_ids = [c["id"] for c in matrix["columns"]]
    prep_all = mx.prepare_races(matrix)          # ← normalize.race_z を通る (train-serve 一貫)
    prep = _slice_races(prep_all, train_from, train_to)

    report = column_sample_report(prep, col_ids)
    warnings = [{"column": cid, "races_passed_gate": n, "threshold": threshold}
                for cid, n in sorted(report.items(), key=lambda kv: kv[1])
                if n < threshold]
    weights = fit_conditional_logit(prep, col_ids, l2=l2, iters=iters, lr=lr)
    return {
        "train_period": [train_from, train_to],
        "display_backtest_from": config.DISPLAY_BACKTEST_FROM,
        "n_races_trained": sum(1 for _ in _race_arrays(prep, col_ids)),
        "weights": weights,
        "column_races_passed_gate": report,
        "low_sample_columns": warnings,
        "mean_log_likelihood_train": round(log_likelihood(prep, col_ids, weights), 6),
        # 学習に効いた欠損ポリシー (サービングと同一実装であることを記録)
        "gate": {"min_horses": nrm.MIN_HORSES, "min_fraction": nrm.MIN_FRACTION},
    }


def save_presets(result: dict, path: str | Path | None = None) -> Path:
    out = Path(path or config.PRESET_WEIGHTS_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out


def load_presets(path: str | Path | None = None) -> dict:
    p = Path(path or config.PRESET_WEIGHTS_PATH)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
