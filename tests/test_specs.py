"""specs (候補特徴量セット) と matrix.merge_matrices のテスト。CIセーフ。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import matrix, model, specs   # noqa: E402


def test_full_and_cheap_specs_valid():
    full = specs.full_specs()
    cheap = specs.cheap_specs()
    assert len(full) > len(cheap)                       # full は compute を含む分多い
    for s in full + cheap:
        assert s["key"] in model.FEATURES               # 全キーが実在
    # aggregate 指定には lookback/match が付く
    aggs = [s for s in full if model.FEATURES[s["key"]].kind == "aggregate"]
    assert aggs and all("lookback" in s and "match" in s for s in aggs)
    # cheap は compute を含まない
    assert all(model.FEATURES[s["key"]].kind != "compute" for s in cheap)
    # 列IDが一意 (重複バリアントで衝突しない)
    ids = [matrix._col_id(s) for s in full]
    assert len(ids) == len(set(ids))


def test_maib_step2_is_exactly_396_columns():
    """設計書 v0.3 §4.2: 9項目 × 一致条件4 × 期間11 = 396 基底列。"""
    s = specs.maib_step2_specs()
    assert len(specs.MAIB_STEP2_METRICS) == 9
    assert len(specs.MAIB_MATCHES) == 4
    assert len(specs.MAIB_LOOKBACKS) == 11
    assert len(s) == 396
    # 列IDが一意 (バリアントが衝突しない)
    ids = [matrix._col_id(x) for x in s]
    assert len(ids) == len(set(ids)) == 396
    # 全キーが実在し、すべて aggregate
    for x in s:
        assert x["key"] in model.FEATURES
        assert model.FEATURES[x["key"]].kind == "aggregate"


def test_maib_step2_covers_the_nine_design_items():
    """設計書の9項目 (賞金・着差・着順・コーナー系5・タイム指数) が揃っていること。"""
    assert set(specs.MAIB_STEP2_METRICS) == {
        "agg_avg_finish", "agg_margin", "agg_time_index", "agg_prize",
        "agg_corner_first", "agg_corner_last", "agg_gain_first_to_last",
        "agg_gain_first_to_finish", "agg_gain_last_to_finish"}


def test_maib_step1_is_curated_and_valid():
    s = specs.maib_step1_specs()
    assert s and len(s) < len(model.FEATURES)      # 全公開しない (認知負荷)
    for x in s:
        assert x["key"] in model.FEATURES
        assert model.FEATURES[x["key"]].kind in ("current", "compute")


def test_maib_all_specs_unique():
    s = specs.maib_all_specs()
    ids = [matrix._col_id(x) for x in s]
    assert len(ids) == len(set(ids))
    assert len(s) == len(specs.maib_step1_specs()) + 396


def test_merge_matrices():
    cols = [{"id": "a", "key": "popularity", "kind": "current", "hib": False,
             "label": "人気", "lookback": None, "match": []}]
    m1 = {"from": "20210101", "to": "20211231", "columns": cols,
          "races": [{"date": "20210601"}]}
    m2 = {"from": "20220101", "to": "20221231", "columns": cols,
          "races": [{"date": "20220601"}, {"date": "20220602"}]}
    merged = matrix.merge_matrices([m1, m2])
    assert len(merged["races"]) == 3
    assert merged["from"] == "20210101" and merged["to"] == "20221231"
    # 列不一致は拒否
    bad = {"from": "x", "to": "y", "columns": [], "races": []}
    with pytest.raises(ValueError):
        matrix.merge_matrices([m1, bad])
