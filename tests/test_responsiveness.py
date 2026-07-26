"""応答性検査 (「選んでも印が動かない」列の検出) のテスト。合成データ・CIセーフ。"""

from __future__ import annotations

import os
import random
import sys

import pytest

pytest.importorskip("numpy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import responsiveness as rp   # noqa: E402

COLS = [
    {"id": "contrarian", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
     "label": "逆張り列", "lookback": 5, "match": []},
    {"id": "follows_pop", "key": "agg_avg_popularity", "kind": "aggregate", "hib": False,
     "label": "人気追随列", "lookback": 5, "match": []},
    {"id": "empty", "key": "agg_prize", "kind": "aggregate", "hib": True,
     "label": "データ無し列", "lookback": 5, "match": []},
]


def _matrix(n_races=60, seed=3) -> dict:
    """contrarian は人気と逆、follows_pop は人気と一致、empty は常に None。"""
    rng = random.Random(seed)
    races = []
    for i in range(n_races):
        n = 8
        horses = []
        for k in range(1, n + 1):
            horses.append({
                "num": str(k), "order": k, "odds": 2.0 + k, "pop": k,
                "x": {"contrarian": float(n - k) + rng.random() * 0.01,  # 人気薄が良い値
                      "follows_pop": float(k),                          # 人気順と同じ
                      "empty": None},
            })
        races.append({"date": "20250801", "seg": {}, "trusted": True,
                      "tan": {"1": 300}, "horses": horses})
    return {"columns": COLS, "races": races}


PRESET_ALL = {"weights": {"contrarian": 1.0, "follows_pop": 1.0, "empty": 1.0}}


def test_detects_responsive_and_flat_columns():
    rep = rp.check_all_cells(_matrix(), PRESET_ALL)
    by = {c["column"]: c for c in rep["cells"]}

    # 人気と逆の列は ◎ がほぼ常に1番人気と異なる → responsive
    assert by["contrarian"]["differs_rate"] == 1.0
    assert by["contrarian"]["responsive"] is True

    # 人気順と同じ列は ◎ が常に1番人気 → 応答なし (選んでも印が動かない)
    assert by["follows_pop"]["differs_rate"] == 0.0
    assert by["follows_pop"]["responsive"] is False
    assert by["follows_pop"]["reason"] == "coefficient_collapsed"

    # 値が無い列は使われない
    assert by["empty"]["races_column_used"] == 0
    assert by["empty"]["responsive"] is False
    assert by["empty"]["reason"] == "column_never_usable"
    assert by["empty"]["flat_rate"] == 1.0


def test_zero_weight_columns_are_reported_as_dead():
    """係数が潰れて0になった列は zero_weight として報告される。"""
    rep = rp.check_all_cells(_matrix(), {"weights": {"contrarian": 1.0}})
    by = {c["column"]: c for c in rep["cells"]}
    assert by["follows_pop"]["reason"] == "zero_weight"
    assert by["follows_pop"]["responsive"] is False
    assert rep["dead_reasons"]["zero_weight"] >= 1


def test_summary_counts_and_rates():
    rep = rp.check_all_cells(_matrix(), PRESET_ALL)
    assert rep["n_cells"] == 3
    assert rep["n_responsive"] == 1 and rep["n_dead"] == 2
    assert rep["dead_rate"] == round(2 / 3, 4)
    assert rep["n_races_evaluated"] == 60
    assert sum(rep["dead_reasons"].values()) == rep["n_dead"]


def test_period_filter_and_race_limit():
    m = _matrix(n_races=10)
    for r in m["races"][:4]:
        r["date"] = "20240101"
    rep = rp.check_all_cells(m, PRESET_ALL, date_from="20250101")
    assert rep["n_races_evaluated"] == 6            # 2024 分は除外
    rep2 = rp.check_all_cells(m, PRESET_ALL, limit_races=3)
    assert rep2["n_races_evaluated"] == 3


def test_unknown_column_is_reported():
    prep_like = []
    got = rp.cell_response(prep_like, COLS, "nope", 1.0)
    assert got["error"] == "unknown_column"
