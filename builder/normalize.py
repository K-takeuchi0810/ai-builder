"""レース内 z-score 正規化と **欠損ポリシー** の単一の実装。

生 JV-Data のカバー範囲は 2025 年以降なので、コーナー系・賞金系の集計は
「直近N走が 2024 年に食い込む馬」で部分欠損する。つまり **当日予想でも欠損は普通に発生する**。
方針を曖昧にすると、少数の馬だけで計算した無意味な z が印を動かしてしまうため、
ここで一度だけ決めて全経路 (scoring / matrix / vsearch) で共有する。

## 欠損ポリシー (確定)

1. **列のレース内カバレッジゲート**: ある列が「値を持つ馬」が
   `MIN_HORSES` 頭未満 または 出走頭数の `MIN_FRACTION` 未満なら、**その列はそのレースで
   一切使わない** (誰の寄与にもしない)。
   理由: 2 頭だけの平均・標準偏差から作った z は意味を持たず、その 2 頭が印を独占してしまう。
2. **使用する列で値が無い馬**: 寄与 **0 (中立)** とし、寄与分解に「データなし」と明示する。
   再正規化 (持っている列だけで割り増す) は **採用しない** — 実績の薄い馬の
   わずかな情報が増幅され、静かに有利になってしまうため。
3. **カバレッジを返す**: 馬ごとに「選択列のうち何列に値があったか」を返し、UI が
   「この馬はコーナーデータが2走分のみ」と表示できるようにする。欠損を隠さず誠実さに変える。
"""

from __future__ import annotations

import math

# 列を使うための最低条件 (レース内)
MIN_HORSES = 4
MIN_FRACTION = 0.5

USE = "used"
SKIP_FEW = "skipped_low_coverage"
SKIP_FLAT = "skipped_no_variance"


def column_decision(values: dict[str, float | None], n_runners: int,
                    min_horses: int = MIN_HORSES,
                    min_fraction: float = MIN_FRACTION) -> tuple[str, int]:
    """列を使うか判定し (判定, 値を持つ馬数) を返す。"""
    have = [v for v in values.values() if v is not None]
    n = len(have)
    if n < max(min_horses, math.ceil(min_fraction * max(n_runners, 1))):
        return SKIP_FEW, n
    if n < 2:
        return SKIP_FEW, n
    mean = sum(have) / n
    var = sum((v - mean) ** 2 for v in have) / n
    if var <= 0:
        return SKIP_FLAT, n
    return USE, n


def race_z(values: dict[str, float | None], higher_is_better: bool,
           n_runners: int | None = None,
           min_horses: int = MIN_HORSES,
           min_fraction: float = MIN_FRACTION) -> tuple[dict[str, float], str, int]:
    """レース内 z-score (向き調整済み) を返す。

    戻り: (馬番→z, 判定, 値を持つ馬数)。判定が USE 以外なら z は空 dict。
    値が無い馬は z に含めない (= 寄与 0 = 中立)。
    """
    runners = n_runners if n_runners is not None else len(values)
    decision, n = column_decision(values, runners, min_horses, min_fraction)
    if decision != USE:
        return {}, decision, n
    have = [v for v in values.values() if v is not None]
    mean = sum(have) / n
    std = (sum((v - mean) ** 2 for v in have) / n) ** 0.5
    direction = 1.0 if higher_is_better else -1.0
    return ({h: direction * ((v - mean) / std) for h, v in values.items() if v is not None},
            USE, n)
