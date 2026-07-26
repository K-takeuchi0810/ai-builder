"""vsearch (numpy ベクトル化探索) のテスト。純Python版との一致を厳密に検証。CIセーフ (numっpy必須)。"""

from __future__ import annotations

import os
import random
import sys

import pytest

pytest.importorskip("numpy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import matrix, vsearch   # noqa: E402


def _synthetic_matrix(seed: int = 7) -> dict:
    rng = random.Random(seed)
    cols = [
        {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
         "label": "平均着順", "lookback": 5, "match": []},
        {"id": "b", "key": "agg_top3_rate", "kind": "aggregate", "hib": True,
         "label": "複勝率", "lookback": 5, "match": ["surface"]},
        {"id": "c", "key": "popularity", "kind": "current", "hib": False,
         "label": "人気", "lookback": None, "match": []},
    ]
    races = []
    for yr in ("2023", "2024", "2025"):
        for _ in range(50):
            n = rng.choice([5, 6, 7])
            win = rng.randrange(n)
            hs = []
            for k in range(n):
                hs.append({"num": str(k + 1), "order": (1 if k == win else k + 2),
                           "odds": round(1.6 + k * 1.4 + rng.random(), 1), "pop": k + 1,
                           "x": {"a": float(rng.randint(1, 12)),
                                 "b": round(rng.random(), 3),
                                 "c": float(k + 1)}})
            races.append({"date": f"{yr}0601", "seg": {}, "trusted": (rng.random() > 0.1),
                          "tan": {hs[win]["num"]: int(hs[win]["odds"] * 100)}, "horses": hs})
    return {"columns": cols, "races": races}


@pytest.mark.parametrize("weights,t,ev", [
    ({"a": 1.0, "b": 0.5, "c": -0.5}, 1.0, 1.1),
    ({"a": -1.0, "c": 1.0}, 0.5, 1.0),
    ({"b": 1.0}, 2.0, 1.3),
])
def test_numpy_matches_pure(weights, t, ev):
    m = _synthetic_matrix()
    prep_pure = matrix.prepare_races(m)
    pure = matrix.value_stats_prepared(prep_pure, weights, t, ev)

    prep_np = vsearch.prepare_numpy(m)
    w = vsearch.weights_to_vec(prep_np, weights)
    got = vsearch.eval_value_np(prep_np, w, t, ev, "00000000", "99999999")

    assert got["bets"] == pure["bets"], (got, pure)
    assert got["roi"] == pure["roi"], (got, pure)
    assert got["hit_rate"] == pure["hit_rate"], (got, pure)


def test_favorite_numpy_matches_pure():
    m = _synthetic_matrix()
    pure = matrix.favorite_stats_prepared(matrix.prepare_races(m))
    prep = vsearch.prepare_numpy(m)
    got = vsearch.favorite_np(prep, "00000000", "99999999")
    assert got["bets"] == pure["bets"]
    assert got["roi"] == pure["roi"]
    assert got["hit_rate"] == pure["hit_rate"]


def test_run_search_np_structure():
    m = _synthetic_matrix()
    prep = vsearch.prepare_numpy(m)
    res = vsearch.run_search_np(
        prep, train=("20230101", "20231231"), valid=("20240101", "20241231"),
        test=("20250101", "20251231"), n_candidates=60, min_bets=3, top_k=5, seed=1)
    assert res["n_tried"] > 0
    assert "favorite_test" in res["baselines"]
    for q in res["top"]:
        assert "test" in q and "roi" in q["valid"]
