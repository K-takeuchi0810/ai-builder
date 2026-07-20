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


@pytest.mark.skipif(not _real_env_ready(), reason="keiba-yosou の DB / lightgbm が無い環境ではスキップ")
def test_build_matrix_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "_CACHE_DIR", tmp_path)
    specs = [
        {"key": "agg_top3_rate", "lookback": 5, "match": ["surface"]},
        {"key": "jockey_win_rate"},
        {"key": "popularity"},
    ]
    m = matrix.build_matrix("20250104", "20250106", specs)
    if not m["races"]:
        pytest.skip("対象期間にレースが無い")
    assert len(m["columns"]) == 3
    r0 = m["races"][0]
    assert set(("date", "seg", "horses", "tan", "trusted")) <= set(r0.keys())
    assert all(cid in r0["horses"][0]["x"] for cid in (c["id"] for c in m["columns"]))
    # キャッシュファイルが書かれ、再読込で同一
    m2 = matrix.build_matrix("20250104", "20250106", specs)
    assert m2["from"] == m["from"] and len(m2["races"]) == len(m["races"])
    # 行列上の評価が動く
    res = matrix.evaluate_matrix(m, {"jockey_win_rate": 1.0, "popularity": 0.5}, "20250105")
    assert res["overall"]["n"] == len(m["races"])
