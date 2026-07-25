"""model (スコアリング) と evaluate (バックテスト) のテスト。

純粋部分 (_surface/_run_matches/_aggregate_value/score_from_features) は CIセーフ。
predict/evaluate の実DB経路は lightgbm+keiba.db のある環境でのみ実行 (skip gate)。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, model   # noqa: E402


# ---------------------------------------------------------------------------
# CIセーフ: 純粋ロジック
# ---------------------------------------------------------------------------
def test_surface_from_track_type():
    assert model._surface("11") == "turf"
    assert model._surface("24") == "dirt"
    assert model._surface("51") == "jump"
    assert model._surface(None) == "other"


def test_run_matches():
    race = {"track_type_code": "11", "track_code": "05", "distance": 1600}
    assert model._run_matches({"track_type_code": "12"}, race, ["surface"]) is True   # 芝どうし
    assert model._run_matches({"track_type_code": "24"}, race, ["surface"]) is False  # ダ vs 芝
    assert model._run_matches({"track_code": "06"}, race, ["track"]) is False
    assert model._run_matches({"distance": 2000}, race, ["distance"]) is False
    assert model._run_matches({"track_code": "05"}, race, ["track"]) is True
    assert model._run_matches({}, race, []) is True                                   # 条件なし


def test_aggregate_value_lookback_and_match():
    feat = model.FEATURES["agg_avg_finish"]
    race = {"track_type_code": "11", "track_code": "05", "distance": 1600}
    # 新しい順。芝: 1着,3着 / ダ: 5着
    past = [
        {"confirmed_order": 1, "track_type_code": "11"},
        {"confirmed_order": 5, "track_type_code": "24"},
        {"confirmed_order": 3, "track_type_code": "12"},
    ]
    assert model._aggregate_value(feat, past, race, ["surface"], None) == 2.0   # (1+3)/2
    assert model._aggregate_value(feat, past, race, ["surface"], 1) == 1.0      # 最新の芝のみ
    assert model._aggregate_value(feat, past, race, [], None) == 3.0            # (1+5+3)/3
    # top3 率
    f3 = model.FEATURES["agg_top3_rate"]
    assert round(model._aggregate_value(f3, past, race, [], None), 3) == 0.667  # 1,0,1
    # 該当なし → None
    assert model._aggregate_value(feat, [], race, [], None) is None


def _rows():
    return {
        "1": {"agg_avg_finish": 2.0, "popularity": 1.0},
        "2": {"agg_avg_finish": 6.0, "popularity": 5.0},
        "3": {"agg_avg_finish": 4.0, "popularity": 3.0},
    }


def test_score_direction_and_weight():
    # 平均着順は小さいほど良い → 馬1が上位
    r = model.score_from_features(_rows(), {"features": [{"key": "agg_avg_finish", "weight": 1.0}]})
    assert r[0][0] == "1" and r[-1][0] == "2"
    # 負の重みで反転
    rn = model.score_from_features(_rows(), {"features": [{"key": "agg_avg_finish", "weight": -1.0}]})
    assert rn[0][0] == "2"
    # popularity も小さいほど良い
    rp = model.score_from_features(_rows(), {"features": [{"key": "popularity", "weight": 1.0}]})
    assert rp[0][0] == "1"


def test_score_none_is_neutral_and_zero_variance_skipped():
    rows = {"1": {"agg_avg_finish": None}, "2": {"agg_avg_finish": 2.0}, "3": {"agg_avg_finish": 6.0}}
    d = dict(model.score_from_features(rows, {"features": [{"key": "agg_avg_finish", "weight": 1.0}]}))
    assert d["2"] > d["1"] > d["3"]              # None の馬1 は中立(0)
    # 分散ゼロは寄与しない
    rows2 = {"1": {"popularity": 3.0}, "2": {"popularity": 3.0}}
    d2 = dict(model.score_from_features(rows2, {"features": [{"key": "popularity", "weight": 1.0}]}))
    assert d2["1"] == 0.0 and d2["2"] == 0.0


def test_time_index_derivation():
    """タイム指数 = 100mあたり走破タイム。専用カラムが無いので導出する。"""
    # 1600m を 96.0秒 (960 = 1/10秒) → 60.0 (1/10秒/100m)
    assert model._time_index({"finish_time": 960, "distance": 1600}) == 60.0
    # 距離が違っても比較可能になる: 2000m を 120.0秒 → 60.0
    assert model._time_index({"finish_time": 1200, "distance": 2000}) == 60.0
    # 欠損・ゼロ距離は None
    assert model._time_index({"finish_time": 0, "distance": 1600}) is None
    assert model._time_index({"finish_time": 960, "distance": 0}) is None
    assert model._time_index({}) is None


def test_derived_metric_readers():
    r = {"_margin_to_winner": 12.0, "_final3_rank": 3, "_final3_n": 15}
    assert model._margin(r) == 12.0
    assert model._final3f_rank(r) == 3.0
    assert model._final3f_rank_ratio(r) == 0.2       # 3/15
    assert model._margin({}) is None
    assert model._final3f_rank_ratio({"_final3_rank": 3}) is None


def _mem_conn_with_race():
    """1レース3頭ぶんの horse_races を持つ in-memory DB。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE horse_races (race_year TEXT, race_month_day TEXT, track_code TEXT,"
        " kaiji TEXT, nichiji TEXT, race_num TEXT, horse_num TEXT, confirmed_order INTEGER,"
        " finish_time INTEGER, final_3f INTEGER)")
    rows = [
        ("2025", "0105", "05", "01", "01", "01", "1", 1, 950, 350),   # 勝ち馬・上がり2位
        ("2025", "0105", "05", "01", "01", "01", "2", 2, 962, 345),   # 着差12・上がり1位
        ("2025", "0105", "05", "01", "01", "01", "3", 3, 975, 360),   # 着差25・上がり3位
    ]
    conn.executemany("INSERT INTO horse_races VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return conn


def test_enrich_past_runs_margin_and_final3_rank():
    conn = _mem_conn_with_race()
    base = {"race_year": "2025", "race_month_day": "0105", "track_code": "05",
            "kaiji": "01", "nichiji": "01", "race_num": "01"}
    runs = [dict(base, horse_num="2", finish_time=962, final_3f=345, distance=1600),
            dict(base, horse_num="3", finish_time=975, final_3f=360, distance=1600)]
    cache: dict = {}
    model._enrich_past_runs(conn, runs, cache)

    assert runs[0]["_margin_to_winner"] == 12.0      # 962 - 950 (勝ち馬)
    assert runs[0]["_final3_rank"] == 1              # 345 が最速
    assert runs[0]["_final3_n"] == 3
    assert runs[1]["_margin_to_winner"] == 25.0
    assert runs[1]["_final3_rank"] == 3
    # 2回目はキャッシュから (レースキーが1つだけ登録されている)
    assert len(cache) == 1


def test_new_aggregate_features_registered():
    for k in ("agg_margin", "agg_time_index", "agg_final3f_rank", "agg_final3f_rank_ratio"):
        assert k in model.FEATURES
        assert model.FEATURES[k].kind == "aggregate"
        assert model.FEATURES[k].higher_is_better is False   # いずれも小さいほど良い


def test_default_config_valid():
    for spec in model.default_config()["features"]:
        assert spec["key"] in model.FEATURES


# ---------------------------------------------------------------------------
# 実DB依存: predict / evaluate のスモーク
# ---------------------------------------------------------------------------
def _real_env_ready() -> bool:
    if not config.KEIBA_DB_PATH.exists():
        return False
    model._ensure_keiba_on_path()
    return importlib.util.find_spec("lightgbm") is not None


requires_real_db = pytest.mark.skipif(
    not _real_env_ready(),
    reason="keiba-yosou の DB / lightgbm が無い環境ではスキップ")


@requires_real_db
def test_evaluate_smoke():
    from builder.evaluate import evaluate
    res = evaluate(model.default_config(), "20250104", "20250106", split_date="20250105")
    for key in ("overall", "train", "holdout"):
        assert key in res
    ov = res["overall"]
    if ov["n"] == 0:
        pytest.skip("対象期間にレースが無い")
    assert 0.0 <= ov["win_rate"] <= 1.0
    assert 0.0 <= ov["top3_rate"] <= 1.0
    assert ov["roi"] is None or ov["roi"] >= 0.0
    # TRAIN + HOLDOUT = overall (件数の整合)
    assert res["train"]["n"] + res["holdout"]["n"] == ov["n"]
