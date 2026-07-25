"""segsearch (セグメント別探索) のテスト。合成データ・CIセーフ。

最重要: ベクトル化した ◎ 抽出 (_picks) と集計が素朴実装と一致すること (実資産がかかるため)。
"""

from __future__ import annotations

import os
import random
import sys

import pytest

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import segsearch, vsearch   # noqa: E402


def _matrix(seed=11) -> dict:
    """4年 × 2セグメント (東京芝1600良 / 中山ダ1200良) の合成レース。"""
    rng = random.Random(seed)
    cols = [
        {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
         "label": "平均着順", "lookback": 5, "match": []},
        {"id": "b", "key": "agg_top3_rate", "kind": "aggregate", "hib": True,
         "label": "複勝率", "lookback": 5, "match": []},
        {"id": "c", "key": "popularity", "kind": "current", "hib": False,
         "label": "人気", "lookback": None, "match": []},
    ]
    segs = [
        {"track": "05", "surface": "turf", "distance": 1600,
         "distance_bucket": "mile", "condition": "firm", "month": "5"},
        {"track": "06", "surface": "dirt", "distance": 1200,
         "distance_bucket": "sprint", "condition": "firm", "month": "5"},
    ]
    races = []
    for yr in ("2023", "2024", "2025", "2026"):
        for i in range(80):
            sg = segs[i % 2]
            n = rng.choice([6, 7, 8])
            win = rng.randrange(n)
            hs = []
            for k in range(n):
                hs.append({"num": str(k + 1), "order": (1 if k == win else k + 2),
                           "odds": round(1.8 + k * 1.5 + rng.random(), 1), "pop": k + 1,
                           "x": {"a": float(rng.randint(1, 12)),
                                 "b": round(rng.random(), 3), "c": float(k + 1)}})
            races.append({"date": f"{yr}0501", "seg": dict(sg), "trusted": True,
                          "tan": {hs[win]["num"]: int(hs[win]["odds"] * 100)},
                          "horses": hs})
    return {"columns": cols, "races": races}


def test_picks_matches_naive():
    """ベクトル化 ◎ 抽出が、レースごとの素朴 argmax と完全一致すること。"""
    prep = vsearch.prepare_numpy(_matrix())
    starts, counts, race_of_row = segsearch._race_geometry(prep)
    rng = np.random.default_rng(0)
    for _ in range(5):
        w = rng.normal(size=len(prep["col_ids"]))
        scores = prep["Z"] @ w
        fast = segsearch._picks(scores, starts, counts, race_of_row)
        naive = np.array([s + int(np.argmax(scores[s:e])) for s, e in prep["offsets"]])
        assert np.array_equal(fast, naive)


def test_seg_key_and_level_codes():
    prep = vsearch.prepare_numpy(_matrix())
    fields = ("track", "surface", "distance", "condition")
    codes, keys = segsearch._level_codes(prep, fields)
    assert sorted(keys) == ["05/turf/1600/firm", "06/dirt/1200/firm"]
    assert len(set(codes.tolist())) == 2
    # global 粒度は 1 セグメント
    _c, gkeys = segsearch._level_codes(prep, ())
    assert gkeys == ["ALL"]


def test_pick_eval_matches_manual():
    """eval_pick_idx の勝率/複勝率/回収率が手計算と一致すること。"""
    m = _matrix()
    prep = vsearch.prepare_numpy(m)
    w = vsearch.weights_to_vec(prep, {"c": 1.0})     # 人気(小さいほど良い) → 1番人気を選ぶ
    idx = list(range(len(m["races"])))
    got = vsearch.eval_pick_idx(prep, w, idx)
    fav = vsearch.favorite_pick_idx(prep, idx)
    # 人気のみの重み = 1番人気を ◎ にする → 市場ベースラインと一致するはず
    assert got["n"] == fav["n"]
    assert got["win_rate"] == fav["win_rate"]
    assert got["roi"] == fav["roi"]


def test_run_segment_search_structure_and_gates():
    prep = vsearch.prepare_numpy(_matrix())
    res = segsearch.run_segment_search(
        prep, train=("20230101", "20231231"), valid=("20240101", "20241231"),
        test=("20250101", "20251231"), final=("20260101", "20261231"),
        n_candidates=40, min_races=10, seed=3, progress_every=0)

    s = res["summary"]
    assert s["objectives"] == ["win_rate", "top3_rate", "roi"]   # 3目的関数すべて
    assert "expected_false_positives_at_valid" in s              # 多重比較の記録
    assert set(res["levels"]) == {name for name, _f in vsearch.SEG_LEVELS}  # 全8粒度

    # 細粒度に 2 セグメントが存在し、各目的関数の結果を持つ
    fine = res["levels"]["track_surface_distance_condition"]["segments"]
    assert set(fine) == {"05/turf/1600/firm", "06/dirt/1200/firm"}
    for key, per_obj in fine.items():
        for o in ("win_rate", "top3_rate", "roi"):
            assert o in per_obj
            r = per_obj[o]
            if r.get("status") == "ok":
                # 採用は「両OOS期間で CI下限がベースライン超え」が必須。
                base = r["beats_favorite_both_ci"] and r["beats_global_both_ci"]
                if o == "roi":   # roi は更に CI下限>1.0 と 大穴くじ除外が必要
                    base = base and r["roi_ci_lower_above_1"] and r["hit_rate_guard_passed"]
                assert r["adopted"] == bool(base)
                assert r["periods"]["test"]["n"] >= 10
                assert "ci_lower" in r and "n_nonzero_weights" in r


def test_wilson_ci_properties():
    lo, hi = segsearch.wilson_ci(5, 10)
    assert 0.0 < lo < 0.5 < hi < 1.0
    # n が増えると区間が狭まる (小標本で過信しない)
    lo_s, hi_s = segsearch.wilson_ci(3, 10)
    lo_b, hi_b = segsearch.wilson_ci(30, 100)
    assert (hi_b - lo_b) < (hi_s - lo_s)
    assert segsearch.wilson_ci(0, 0) == (0.0, 1.0)


def test_bootstrap_roi_ci_heavy_tail():
    # 43レース中 2本だけ25倍的中 → 点推定 ROI は 1.16 でも CI 下限は 1 を大きく下回る
    ret = np.zeros(43)
    ret[:2] = 25.0
    point = ret.mean()
    lo, hi = segsearch.bootstrap_roi_ci(ret, n_boot=1000, seed=1)
    assert point > 1.0
    assert lo < 1.0            # まぐれ当たりは CI 下限で弾ける
    assert lo <= point <= hi
    assert segsearch.bootstrap_roi_ci(np.array([]), n_boot=10) == (0.0, 0.0)


def test_sparsity_sweep_recorded():
    prep = vsearch.prepare_numpy(_matrix())
    res = segsearch.run_segment_search(
        prep, train=("20230101", "20231231"), valid=("20240101", "20241231"),
        test=("20250101", "20251231"), final=("20260101", "20261231"),
        n_candidates=21, min_races=10, sparsity_levels=(1, 2, None),
        seed=5, progress_every=0)
    s = res["summary"]
    assert s["sparsity_levels"] == [1, 2, "dense"]
    assert s["nonzero_weight_min"] == 1          # 疎な候補が実際に生成されている
    assert s["nonzero_weight_max"] >= 2
    assert "adoption_rule" in s


def test_month_and_season_levels_present():
    """ユーザー例「8月×新潟芝1800m良」の粒度が探索対象に含まれていること。"""
    names = {n for n, _f in vsearch.SEG_LEVELS}
    assert "track_surface_distance_condition_month" in names
    assert "track_surface_distance_condition_season" in names
    # season は month から派生する仮想フィールド
    assert vsearch.seg_field({"month": "8"}, "season") == "summer"
    assert vsearch.seg_key({"track": "04", "surface": "turf", "distance": 1800,
                            "condition": "firm", "month": "8"},
                           ("track", "surface", "distance", "condition", "month")) \
        == "04/turf/1800/firm/8"


def test_restrict_months_makes_periods_like_for_like():
    """部分年を OOS に使うための月限定が、対象レースとベースラインの両方に効くこと。"""
    m = _matrix()
    # 5月のみのデータなので、5月に限定すれば全件、8月に限定すれば0件になる
    prep = vsearch.prepare_numpy(m)
    common = dict(train=("20230101", "20231231"), valid=("20240101", "20241231"),
                  test=("20250101", "20251231"), final=("20260101", "20261231"),
                  n_candidates=12, min_races=5, sparsity_levels=(2,), seed=2,
                  progress_every=0)
    keep = segsearch.run_segment_search(prep, restrict_months=(5,), **common)
    drop = segsearch.run_segment_search(prep, restrict_months=(8,), **common)

    assert keep["summary"]["restrict_months"] == [5]
    fine_keep = keep["levels"]["track_surface_distance_condition"]["segments"]
    ok_keep = sum(1 for sv in fine_keep.values() for r in sv.values()
                  if isinstance(r, dict) and r.get("status") == "ok")
    assert ok_keep > 0                      # 5月限定なら評価できる

    fine_drop = drop["levels"]["track_surface_distance_condition"]["segments"]
    for sv in fine_drop.values():           # 8月限定なら該当レース0 → 全てデータ不足
        for r in sv.values():
            if isinstance(r, dict):
                assert r.get("status") == "insufficient_data"
    assert drop["summary"]["n_adopted"] == 0


def test_insufficient_data_marked():
    """min_races を大きくすると全セグメントがデータ不足として扱われること。"""
    prep = vsearch.prepare_numpy(_matrix())
    res = segsearch.run_segment_search(
        prep, train=("20230101", "20231231"), valid=("20240101", "20241231"),
        test=("20250101", "20251231"), final=("20260101", "20261231"),
        n_candidates=10, min_races=10_000, seed=1, progress_every=0)
    fine = res["levels"]["track_surface_distance_condition"]["segments"]
    for per_obj in fine.values():
        for o in ("win_rate", "top3_rate", "roi"):
            assert per_obj[o]["status"] == "insufficient_data"
    assert res["summary"]["n_adopted"] == 0
