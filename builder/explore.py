"""探索エンジン (pure core) — セグメント別の的中率/回収率/calibration を
hold-out 分離 + Wilson CI + 過学習ガード付きで算出する。

**設計方針 (netkeiba builder との決定的な違い)**:
netkeiba 型の builder は「過去データで回収率が高い条件」を探させるが、それを
そのまま買い目にすると過学習事故 (keiba-yosou P12: TEST 通年 184% → PRODUCTION 45%) を
再現する。本エンジンは探索(TRAIN)と検証(HOLDOUT)を API レベルで強制分離し、
「TRAIN で有意 かつ HOLDOUT で符号再現」したセルだけを reproduced=True とする。
サンプル数ゲート + Wilson CI で、n 不足のセルは「バイアス」と呼ばない。

このモジュールは keiba-yosou にも DB にも依存しない純粋関数のみ。実データからの
Sample 生成は builder/keiba_bridge.py が担い、本モジュールはそれをテスト可能に保つ。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Sample:
    """探索の 1 レコード。1 レース 1 頭 (◎ pick なら 1 レース 1 件)。

    axes は「軸名 → 値」の辞書 (track/surface/condition/weather_wet/month/
    sire_line/sire_country/popularity 等)。値は文字列 or 数値文字列。
    """

    date: str                      # "YYYYMMDD"。hold-out 分割のキー
    axes: dict[str, str]           # 軸名 → 値
    pred: float                    # 予測勝率 (keiba-yosou raw_blended_probability)
    won: int                       # 1=1着, 0=それ以外
    top3: int = 0                  # 1=3着以内
    ret: float | None = None       # 単位賭けの払戻 (1.0=元返し, 0.0=はずれ)。オッズ不信頼なら None


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """勝率の Wilson 信頼区間。n=0 は (0,0)。"""
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _to_num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_filter(sample: Sample, f: dict) -> bool:
    """1 フィルタ条件にマッチするか。f = {axis, op, value}。
    op: eq / in / gte / lte / range(value=[lo,hi])。"""
    v = sample.axes.get(f["axis"])
    if v is None:
        return False
    op = f["op"]
    target = f["value"]
    if op == "eq":
        return v == target
    if op == "in":
        return v in target
    nv = _to_num(v)
    if nv is None:
        return False
    if op == "gte":
        return nv >= float(target)
    if op == "lte":
        return nv <= float(target)
    if op == "range":
        lo, hi = target
        return float(lo) <= nv <= float(hi)
    raise ValueError(f"unknown op: {op}")


def apply_filters(samples: list[Sample], filters: list[dict]) -> list[Sample]:
    """全条件 (AND) にマッチする Sample だけ返す。filters 空なら全件。"""
    if not filters:
        return list(samples)
    return [s for s in samples if all(match_filter(s, f) for f in filters)]


def summarize_cell(samples: list[Sample], min_n: int) -> dict:
    """セル (Sample 集合) の統計。calibration_gap = mean_pred - actual_rate。
    正 = 過信 (主張ほど勝てない)。gap_significant = mean_pred が実勝率の Wilson 区間外。"""
    n = len(samples)
    if n == 0:
        return {"n": 0, "status": "empty"}
    preds = [s.pred for s in samples]
    wins = sum(s.won for s in samples)
    mean_pred = sum(preds) / n
    actual_rate = wins / n
    gap = mean_pred - actual_rate
    lo, hi = wilson_ci(wins, n)
    gap_significant = mean_pred < lo or mean_pred > hi
    top3_rate = sum(s.top3 for s in samples) / n
    rets = [s.ret for s in samples if s.ret is not None]
    return_pct = (sum(rets) / len(rets)) if rets else None
    return {
        "n": n,
        "mean_pred": mean_pred,
        "actual_rate": actual_rate,
        "calibration_gap": gap,
        "ci_lo": lo,
        "ci_hi": hi,
        "gap_significant": gap_significant,
        "win_pct": actual_rate,
        "top3_pct": top3_rate,
        "return_pct": return_pct,
        "return_n": len(rets),
        "bias_severity": abs(gap) * math.sqrt(n),
        "status": "ok" if n >= min_n else "insufficient",
    }


def _group_by_axis(samples: list[Sample], axis: str) -> dict[str, list[Sample]]:
    groups: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        groups[s.axes.get(axis, "unknown")].append(s)
    return groups


def explore(
    samples: list[Sample],
    axis: str,
    split_date: str,
    filters: list[dict] | None = None,
    min_n: int = 50,
) -> dict:
    """1 軸の探索。filters (AND) で母集団を絞り、split_date で TRAIN/HOLDOUT に分け、
    軸値ごとにセル統計を出す。**reproduced=True** = TRAIN で有意 (n≥min_n かつ
    gap_significant) かつ HOLDOUT でも n≥min_n・有意・gap の符号が一致。これだけが
    「本物のバイアス候補」。TRAIN のみ有意 or HOLDOUT で有意性消失のセルは reproduced=False。

    戻り値: {axis, split_date, min_n, filters, global_train, global_holdout, cells:[...]}
    cells は TRAIN の bias_severity 降順。各 cell = {value, train, holdout, reproduced}。
    """
    filters = filters or []
    filtered = apply_filters(samples, filters)
    train = [s for s in filtered if s.date < split_date]
    holdout = [s for s in filtered if s.date >= split_date]

    train_groups = _group_by_axis(train, axis)
    holdout_groups = _group_by_axis(holdout, axis)

    cells = []
    for value, tsamples in train_groups.items():
        tc = summarize_cell(tsamples, min_n)
        hc = summarize_cell(holdout_groups.get(value, []), min_n)
        # reproduced = TRAIN で有意 かつ HOLDOUT でも n≥min_n・**有意**・gap 符号一致。
        # HOLDOUT で有意性が消える (バイアスが再現しない) セルは弾く — これが過学習ガードの核心。
        reproduced = bool(
            tc.get("status") == "ok"
            and tc.get("gap_significant")
            and hc.get("status") == "ok"
            and hc.get("gap_significant")
            and (tc["calibration_gap"] >= 0) == (hc["calibration_gap"] >= 0)
        )
        cells.append({"value": value, "train": tc, "holdout": hc, "reproduced": reproduced})

    cells.sort(key=lambda c: c["train"].get("bias_severity", 0.0), reverse=True)
    return {
        "axis": axis,
        "split_date": split_date,
        "min_n": min_n,
        "filters": filters,
        "global_train": summarize_cell(train, min_n),
        "global_holdout": summarize_cell(holdout, min_n),
        "cells": cells,
    }
