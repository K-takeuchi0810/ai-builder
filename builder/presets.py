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


def columns_fingerprint(col_ids: list[str]) -> str:
    """列構成の指紋。学習時とサービング時の列構成一致を検証するために使う。"""
    import hashlib
    payload = "|".join(sorted(col_ids))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def check_preset_matches_spec(preset: dict, col_ids: list[str]) -> dict | None:
    """プリセット重みが現在の列構成のものか検証する。不一致なら警告 dict を返す。

    デモ当日に旧モデル (別の列構成で学習した重み) を掴む事故を構造的に防ぐ。
    """
    if not preset:
        return {"code": "no_preset_weights",
                "message": "プリセット重みが未学習です (印は無意味です)",
                "hint": "この列構成で presets.fit_presets を実行してください"}
    want = columns_fingerprint(col_ids)
    got = preset.get("columns_fingerprint")
    if got and got != want:
        return {"code": "preset_column_mismatch",
                "message": "プリセット重みの列構成が現在の設定と一致しません (旧モデルの可能性)",
                "hint": f"期待={want} 実際={got}。現在の spec で再学習してください",
                "expected_fingerprint": want, "actual_fingerprint": got,
                "preset_n_columns": preset.get("n_columns")}
    return None


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


def _ll_and_grad(races, w, l2: float):
    """平均対数尤度と、その勾配 (L2 込み) を返す。"""
    ll = 0.0
    grad = np.zeros_like(w)
    for Z, win in races:
        s = Z @ w
        s -= s.max()
        e = np.exp(s)
        p = e / e.sum()
        ll += math.log(max(p[win], 1e-12))
        grad += Z[win] - (p @ Z)
    n = len(races)
    reg = 0.5 * l2 * float(w @ w) / n
    return ll / n - reg, grad / n - l2 * w / n


def fit_conditional_logit(prep_races: list[dict], col_ids: list[str], *,
                          l2: float = 1.0, iters: int = 200, lr: float = 0.1,
                          tol: float = 1e-7) -> dict:
    """conditional logit を学習し {col_id: weight} を返す。

    ゲートを通らなかった列は z が全て 0 なので勾配も 0 → 重みは初期値 0 のまま。
    (= カレンダーで窓を切らずに「充填レースからのみ学習」が自動的に成立する)

    **単調改善を保証する**: 目的関数は凹だが固定学習率では発散し得る (実測: lr=0.5 で
    平均対数尤度が一様分布より悪化した)。各反復で対数尤度が下がったらステップを半分に
    戻すバックトラッキングを入れ、改善が tol 未満で打ち切る。
    """
    races = list(_race_arrays(prep_races, col_ids))
    w = np.zeros(len(col_ids), dtype=np.float64)
    if not races:
        return {cid: 0.0 for cid in col_ids}

    ll, grad = _ll_and_grad(races, w, l2)
    step = lr
    for _ in range(iters):
        improved = False
        for _try in range(30):                    # ステップを縮めながら改善点を探す
            cand = w + step * grad
            cand_ll, cand_grad = _ll_and_grad(races, cand, l2)
            if cand_ll > ll:
                gain = cand_ll - ll
                w, ll, grad = cand, cand_ll, cand_grad
                improved = True
                break
            step *= 0.5                           # 発散したので後退
        if not improved or gain < tol:
            break
        step *= 1.2                               # 順調なら少しだけ伸ばす
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


def confidence_thresholds(prep_races: list[dict], col_ids: list[str],
                          weights: dict[str, float]) -> dict:
    """1位と2位のスコア差の分位点。設計書 §6 の自信度3段階の閾値を事前決定する。

    「鉄板級」= 上位1/3、「有力」= 中位、「混戦」= 下位1/3。
    """
    w = np.array([weights.get(cid, 0.0) for cid in col_ids], dtype=np.float64)
    gaps = []
    for Z, _win in _race_arrays(prep_races, col_ids):
        s = np.sort(Z @ w)[::-1]
        if s.size >= 2:
            gaps.append(float(s[0] - s[1]))
    if not gaps:
        return {"solid": None, "strong": None, "n": 0}
    q33, q67 = (float(x) for x in np.quantile(gaps, [1 / 3, 2 / 3]))
    return {"solid": round(q67, 6), "strong": round(q33, 6), "n": len(gaps)}


def confidence_label(gap: float | None, thresholds: dict) -> str:
    """スコア差 → 自信度ラベル (鉄板級 / 有力 / 混戦)。閾値が無ければ「—」。"""
    solid, strong = thresholds.get("solid"), thresholds.get("strong")
    if gap is None or solid is None or strong is None:
        return "—"
    if gap >= solid:
        return "鉄板級"
    if gap >= strong:
        return "有力"
    return "混戦"


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
        # 自信度3段階の閾値 (学習期間の分位点で事前決定。設計書 §6)
        "confidence_thresholds": confidence_thresholds(prep, col_ids, weights),
        # 学習に効いた欠損ポリシー (サービングと同一実装であることを記録)
        "gate": {"min_horses": nrm.MIN_HORSES, "min_fraction": nrm.MIN_FRACTION},
        # 列構成の指紋。サービング時に spec と一致するか検証し、旧モデル誤用を構造的に防ぐ
        "n_columns": len(col_ids),
        "columns_fingerprint": columns_fingerprint(col_ids),
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
