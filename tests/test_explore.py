"""explore コア + axes 導出の単体テスト (keiba-yosou 不要、合成データのみ)。"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import axes as ax          # noqa: E402
from builder.explore import (            # noqa: E402
    Sample, apply_filters, explore, summarize_cell, wilson_ci,
)


def test_wilson_ci_basic():
    lo, hi = wilson_ci(0, 0)
    assert (lo, hi) == (0.0, 0.0)
    lo, hi = wilson_ci(5, 10)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    # n が増えると区間が狭まる
    _, hi_small = wilson_ci(3, 10)
    _, hi_big = wilson_ci(30, 100)
    assert (hi_big - 0.3) < (hi_small - 0.3)


def test_summarize_cell_gap_and_gate():
    # mean_pred=0.5 だが実勝率 0.2 → 過信 (gap 正)
    samples = [Sample("20240101", {}, 0.5, won=(1 if i < 2 else 0)) for i in range(10)]
    s = summarize_cell(samples, min_n=5)
    assert s["n"] == 10
    assert abs(s["mean_pred"] - 0.5) < 1e-9
    assert abs(s["actual_rate"] - 0.2) < 1e-9
    assert s["calibration_gap"] > 0
    assert s["status"] == "ok"
    # min_n 未満は insufficient
    assert summarize_cell(samples, min_n=50)["status"] == "insufficient"
    # 空セル
    assert summarize_cell([], min_n=5)["status"] == "empty"


def test_apply_filters_ops():
    s = Sample("20240101", {"surface": "turf", "distance_m": "1600", "popularity": "3"}, 0.3, 0)
    assert apply_filters([s], [{"axis": "surface", "op": "eq", "value": "turf"}]) == [s]
    assert apply_filters([s], [{"axis": "surface", "op": "eq", "value": "dirt"}]) == []
    assert apply_filters([s], [{"axis": "distance_m", "op": "gte", "value": 1500}]) == [s]
    assert apply_filters([s], [{"axis": "distance_m", "op": "lte", "value": 1500}]) == []
    assert apply_filters([s], [{"axis": "distance_m", "op": "range", "value": [1400, 1800]}]) == [s]
    assert apply_filters([s], [{"axis": "surface", "op": "in", "value": ["turf", "jump"]}]) == [s]
    # フィルタ空は全件
    assert apply_filters([s], []) == [s]


def _biased_samples():
    """soft は両期間で過信 (reproduced 期待)、snow は TRAIN のみノイズ (非 reproduced 期待)。"""
    out = []
    # soft: TRAIN 200 件, pred 0.5, actual 0.2 (両期間)
    for period, yr in (("t", "2024"), ("h", "2025")):
        for i in range(200):
            out.append(Sample(f"{yr}0101", {"cond": "soft"}, 0.5, won=(1 if i < 40 else 0)))
        # firm: 両期間で calibration 一致 (gap ~ 0)
        for i in range(200):
            out.append(Sample(f"{yr}0101", {"cond": "firm"}, 0.3, won=(1 if i < 60 else 0)))
    # snow: TRAIN だけ過信、HOLDOUT は一致
    for i in range(200):
        out.append(Sample("20240101", {"cond": "snow"}, 0.5, won=(1 if i < 40 else 0)))
    for i in range(200):
        out.append(Sample("20250101", {"cond": "snow"}, 0.5, won=(1 if i < 100 else 0)))
    return out


def test_explore_reproduced_gate():
    rep = explore(_biased_samples(), axis="cond", split_date="20250101", min_n=50)
    cells = {c["value"]: c for c in rep["cells"]}
    # soft: TRAIN 有意 + HOLDOUT 同符号 → reproduced
    assert cells["soft"]["reproduced"] is True
    assert cells["soft"]["train"]["calibration_gap"] > 0
    # snow: TRAIN のみ過信、HOLDOUT では実勝率 0.5 で gap≈0 → 非 reproduced
    assert cells["snow"]["reproduced"] is False
    # firm: そもそも有意でない → 非 reproduced
    assert cells["firm"]["reproduced"] is False


def test_explore_holdout_split():
    samples = [Sample("20240601", {"x": "a"}, 0.3, 0), Sample("20250601", {"x": "a"}, 0.3, 0)]
    rep = explore(samples, axis="x", split_date="20250101", min_n=1)
    assert rep["global_train"]["n"] == 1
    assert rep["global_holdout"]["n"] == 1


def test_axes_derive():
    a = ax.derive_axes(
        track="05", surface="turf", distance=1600,
        turf_condition="3", dirt_condition="0", weather_code="4",
        race_month_day="0705", kaiji="03", nichiji="08", popularity=2,
        sire_line="サンデー系", sire_country="日本型",
        dam_sire_line="ノーザンD系", dam_sire_country="欧州型",
    )
    assert a["surface"] == "turf"
    assert a["distance"] == "mile"
    assert a["condition"] == "yielding"     # turf_condition=3
    assert a["weather"] == "rain" and a["weather_wet"] == "wet"
    assert a["meet_progress"] == "late"     # nichiji 8
    assert a["season"] == "summer"          # 07 月
    assert a["popularity"] == "2"
    assert a["sire_line"] == "サンデー系"


def test_axes_buckets():
    assert ax.distance_bucket(1200) == "sprint"
    assert ax.distance_bucket(1600) == "mile"
    assert ax.distance_bucket(2000) == "middle"
    assert ax.distance_bucket(2500) == "long"
    assert ax.distance_bucket(None) == "unknown"
    assert ax.popularity_band(1) == "1" and ax.popularity_band(5) == "4-6"
    assert ax.popularity_band(12) == "10+" and ax.popularity_band(None) == "unknown"
    assert ax.season_of("01") == "winter" and ax.season_of("04") == "spring"
