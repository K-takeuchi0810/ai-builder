"""search (バリューベットの重み探索 + 3分割OOS) のテスト。合成行列のみ・CIセーフ。"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import search   # noqa: E402


def _synthetic_matrix(seed: int = 1) -> dict:
    """2022-2025 の合成レース。特徴 a が着順と相関し、オッズも付与。"""
    rng = random.Random(seed)
    cols = [
        {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
         "label": "平均着順", "lookback": 5, "match": []},
        {"id": "b", "key": "popularity", "kind": "current", "hib": False,
         "label": "人気", "lookback": None, "match": []},
    ]
    races = []
    for yr in ("2022", "2023", "2024", "2025"):
        for i in range(60):
            n = 6
            # a: 小さいほど強い。勝ち馬を a 最小付近に寄せる
            avals = rng.sample(range(1, 12), n)
            winner = min(range(n), key=lambda k: avals[k])
            horses = []
            for k in range(n):
                pop = k + 1
                odds = round(1.5 + pop * 1.3 + rng.random(), 1)
                horses.append({"num": str(k + 1), "order": (1 if k == winner else k + 2),
                               "odds": odds, "pop": pop, "x": {"a": float(avals[k])}})
            tan = {horses[winner]["num"]: int(horses[winner]["odds"] * 100)}
            races.append({"date": f"{yr}0601", "seg": {}, "trusted": True,
                          "tan": tan, "horses": horses})
    return {"columns": cols, "races": races}


def test_run_search_structure_and_oos():
    m = _synthetic_matrix()
    res = search.run_search(
        m, train=("20220101", "20221231"), valid=("20230101", "20231231"),
        test=("20240101", "20241231"), final=("20250101", "20251231"),
        n_candidates=80, min_bets=5, top_k=5, seed=0)

    assert res["n_tried"] > 0
    assert res["n_qualified"] >= 0
    assert "favorite_test" in res["baselines"]
    assert res["periods"]["test"] == ("20240101", "20241231")
    # 選ばれた候補は TEST/final が評価済み (選択は VALID のみで実施)
    for q in res["top"]:
        assert "test" in q and "final" in q
        assert "roi" in q["valid"] and "roi" in q["test"]
    # 多重比較の記録がある
    assert "expected_false_positives_at_valid" in res


def test_favorite_baseline():
    from builder import matrix
    m = _synthetic_matrix()
    prep = matrix.prepare_races(m)
    tr = search._slice(prep, "20220101", "20221231")
    fb = search.favorite_baseline(tr)
    assert fb["bets"] == 60
    assert 0.0 <= fb["hit_rate"] <= 1.0


def test_slice_bounds():
    races = [{"date": "20220601"}, {"date": "20230601"}, {"date": "20240601"}]
    assert len(search._slice(races, "20230101", "20231231")) == 1
    assert len(search._slice(races, "20220101", "20241231")) == 3
