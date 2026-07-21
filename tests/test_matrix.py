"""matrix (特徴量行列の事前計算 + 行列上評価) のテスト。

純粋部分 (score_race_columns / evaluate_matrix) は CIセーフ。build_matrix の実DB経路は
lightgbm + keiba.db のある環境でのみ実行 (skip gate)。
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, matrix, model   # noqa: E402


_COLS = [
    {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
     "label": "平均着順", "lookback": 5, "match": []},
    {"id": "b", "key": "jockey_win_rate", "kind": "compute", "hib": True,
     "label": "騎手勝率", "lookback": None, "match": []},
]


def _hrows():
    return [
        {"num": "1", "order": 1, "x": {"a": 2.0, "b": 0.30}},
        {"num": "2", "order": 5, "x": {"a": 6.0, "b": 0.10}},
        {"num": "3", "order": 3, "x": {"a": 4.0, "b": 0.20}},
    ]


def test_score_race_columns_direction():
    # a=平均着順(小さいほど良い) → 馬1が上位
    r = matrix.score_race_columns(_hrows(), _COLS, {"a": 1.0})
    assert r[0][0] == "1" and r[-1][0] == "2"
    # b=騎手勝率(大きいほど良い) → 馬1が上位
    r2 = matrix.score_race_columns(_hrows(), _COLS, {"b": 1.0})
    assert r2[0][0] == "1"
    # 重み0は無視、未知idは無視
    r3 = matrix.score_race_columns(_hrows(), _COLS, {"a": 0.0, "zzz": 9.0})
    assert all(s == 0.0 for _, s in r3)


def test_evaluate_matrix_split_and_metrics():
    m = {"columns": _COLS, "races": [
        {"date": "20240101", "seg": {}, "horses": _hrows(),
         "tan": {"1": 250}, "trusted": True},                       # pick=1(1着) 勝ち, ret2.5
        {"date": "20250101", "seg": {}, "horses": [
            {"num": "1", "order": 4, "x": {"a": 6.0, "b": 0.1}},
            {"num": "2", "order": 2, "x": {"a": 2.0, "b": 0.3}},    # pick=2(2着) 複勝, tan無し
        ], "tan": {}, "trusted": True},
    ]}
    res = matrix.evaluate_matrix(m, {"a": 1.0}, split_date="20250101")
    assert res["overall"]["n"] == 2
    assert res["train"]["n"] == 1 and res["holdout"]["n"] == 1
    assert res["train"]["win_rate"] == 1.0            # 20240101: 馬1が1着
    assert res["holdout"]["win_rate"] == 0.0          # 20250101: 馬2は2着
    assert res["holdout"]["top3_rate"] == 1.0
    assert res["train"]["roi"] == 2.5                 # 250円/100
    # trusted=False なら回収率は None (ret_n=0)
    m["races"][0]["trusted"] = False
    res2 = matrix.evaluate_matrix(m, {"a": 1.0}, split_date="20250101")
    assert res2["train"]["roi"] is None


def test_softmax_props():
    p = matrix._softmax([("a", 1.0), ("b", -1.0)], 1.0)
    assert abs(sum(p.values()) - 1.0) < 1e-9
    assert p["a"] > p["b"]                       # 高スコアほど高確率


def test_evaluate_value_matrix_ev_selection():
    cols = [{"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
             "label": "平均着順", "lookback": 5, "match": []}]
    # 2頭: 馬1(平均着順2=良, オッズ3.0, 1着), 馬2(平均着順6, オッズ10.0, 2着)
    m = {"columns": cols, "races": [
        {"date": "20240101", "seg": {}, "trusted": True, "tan": {"1": 300}, "horses": [
            {"num": "1", "order": 1, "odds": 3.0, "x": {"a": 2.0}},
            {"num": "2", "order": 2, "odds": 10.0, "x": {"a": 6.0}},
        ]},
    ]}
    # softmax(t=1) で p1≈0.881 → EV1≈2.64, EV2≈1.19。閾値2.0 なら馬1のみ購入。
    res = matrix.evaluate_value_matrix(m, {"a": 1.0}, split_date="20250101",
                                       temperature=1.0, ev_threshold=2.0)
    assert res["train"]["bets"] == 1
    assert res["train"]["hit_rate"] == 1.0
    assert res["train"]["roi"] == 3.0            # 300円払戻 / 100円賭け
    assert res["overall"]["races_with_bet"] == 1
    # trusted=False は購入対象外
    m["races"][0]["trusted"] = False
    res2 = matrix.evaluate_value_matrix(m, {"a": 1.0}, split_date="20250101",
                                        temperature=1.0, ev_threshold=2.0)
    assert res2["overall"]["bets"] == 0


def test_prepared_matches_direct_evaluation():
    """高速経路(prepared)が低速経路(evaluate_value_matrix)と同一結果を出すこと。"""
    cols = [
        {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
         "label": "平均着順", "lookback": 5, "match": []},
        {"id": "b", "key": "popularity", "kind": "current", "hib": False,
         "label": "人気", "lookback": None, "match": []},
    ]
    import random
    rng = random.Random(3)
    races = []
    for yr in ("2024", "2025"):
        for _ in range(40):
            n = 6
            hs = []
            win = rng.randrange(n)
            for k in range(n):
                hs.append({"num": str(k + 1), "order": (1 if k == win else k + 2),
                           "odds": round(1.5 + k * 1.2 + rng.random(), 1), "pop": k + 1,
                           "x": {"a": float(rng.randint(1, 10)), "b": float(k + 1)}})
            races.append({"date": f"{yr}0601", "seg": {}, "trusted": True,
                          "tan": {hs[win]["num"]: int(hs[win]["odds"] * 100)}, "horses": hs})
    m = {"columns": cols, "races": races}
    weights, t, ev = {"a": 1.0, "b": -0.5}, 1.0, 1.1

    direct = matrix.evaluate_value_matrix(m, weights, "99999999", t, ev)["train"]
    prep = matrix.prepare_races(m)
    fast = matrix.value_stats_prepared(prep, weights, t, ev)
    assert direct["bets"] == fast["bets"]
    assert direct["roi"] == fast["roi"]
    assert direct["hit_rate"] == fast["hit_rate"]


def test_col_id_stable_for_aggregate_variants():
    a5 = matrix._col_id({"key": "agg_top3_rate", "lookback": 5, "match": ["surface"]})
    a3 = matrix._col_id({"key": "agg_top3_rate", "lookback": 3, "match": ["surface"]})
    assert a5 != a3                                   # lookback 違いは別カラム
    assert matrix._col_id({"key": "jockey_win_rate"}) == "jockey_win_rate"


# ---------------------------------------------------------------------------
# 実DB依存: build_matrix のスモーク
# ---------------------------------------------------------------------------
def _real_env_ready() -> bool:
    if not config.KEIBA_DB_PATH.exists():
        return False
    model._ensure_keiba_on_path()
    return importlib.util.find_spec("lightgbm") is not None


@pytest.mark.skipif(not _real_env_ready(), reason="keiba-yosou (popularity_config) が無い環境ではスキップ")
def test_build_matrix_monthly_checkpoint(tmp_path, monkeypatch):
    """月次チェックポイント・再開・範囲クリップを検証 (DB compute はモックで高速化)。"""
    import contextlib
    monkeypatch.setattr(matrix, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(matrix, "open_conn", lambda: contextlib.nullcontext(None))

    calls = []

    def fake_range(conn, lo, hi, cols, nc, npast, ma):
        calls.append((lo, hi))
        return [{"date": lo, "seg": {}, "trusted": True, "tan": {"1": 200},
                 "horses": [{"num": "1", "order": 1, "odds": 2.0, "pop": 1,
                             "x": {c["id"]: 1.0 for c in cols}}]}]

    monkeypatch.setattr(matrix, "_races_for_range", fake_range)
    specs = [{"key": "popularity"}]

    # 2 か月ぶん構築 → Jan, Feb の 2 回だけ _races_for_range が呼ばれる
    m = matrix.build_matrix("20240101", "20240228", specs)
    assert calls == [("20240101", "20240131"), ("20240201", "20240231")]
    assert len(m["races"]) == 2
    monthfiles = list((tmp_path / "months").glob("m_*.json"))
    assert len(monthfiles) == 2                       # 月ごとにチェックポイント保存

    # 範囲キャッシュを消し、月キャッシュから再開 → 追加の compute 呼び出し無し
    for f in tmp_path.glob("matrix_*.json"):
        f.unlink()
    calls.clear()
    m2 = matrix.build_matrix("20240101", "20240228", specs)
    assert calls == []                                # 全月キャッシュ済み → 再計算なし
    assert len(m2["races"]) == 2

    # 範囲クリップ: 1 月だけ要求すると Jan の 1 レースのみ (月キャッシュ再利用)
    calls.clear()
    m3 = matrix.build_matrix("20240101", "20240131", specs)
    assert calls == []
    assert [r["date"] for r in m3["races"]] == ["20240101"]
