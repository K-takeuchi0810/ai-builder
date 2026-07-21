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
