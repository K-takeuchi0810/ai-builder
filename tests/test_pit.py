"""PIT (Point-In-Time) ゲートのテスト。

keiba-yosou の v5 leak 事故 (レース後確定変数の混入) と同型のリスクを構造的に塞ぐ:
**対象レース自身のコーナー順位・賞金・着順が、その馬の「過去走集計」に混入してはならない。**

対象レース日より厳密に前 (`<`) のレースだけが過去走として取得されることを固定する。
将来 `<=` に緩むとこのテストが落ちる。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, model   # noqa: E402

pytestmark = pytest.mark.skipif(
    not (config.KEIBA_YOSOU_PATH / "predictor" / "features.py").exists(),
    reason="keiba-yosou が無い環境ではスキップ (horse_past_runs を使う)")

BRN = "2020100001"
TARGET_DATE = "20250601"          # 対象レース日


def _pit_conn() -> sqlite3.Connection:
    """同一馬が 3 レース (対象日・対象日の別R・過去) に出走した in-memory DB。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE horse_races (race_year TEXT, race_month_day TEXT, track_code TEXT,"
        " kaiji TEXT, nichiji TEXT, race_num TEXT, horse_num TEXT, blood_register_num TEXT,"
        " confirmed_order INTEGER, finish_time INTEGER, final_3f INTEGER,"
        " win_popularity INTEGER, win_odds INTEGER, leg_quality_code TEXT,"
        " burden_weight INTEGER, horse_weight TEXT, weight_change_diff TEXT)")
    conn.execute(
        "CREATE TABLE races (race_year TEXT, race_month_day TEXT, track_code TEXT,"
        " kaiji TEXT, nichiji TEXT, race_num TEXT, distance INTEGER, track_type_code TEXT,"
        " grade_code TEXT, race_symbol_code TEXT, starter_count INTEGER, weather_code TEXT,"
        " turf_condition TEXT, dirt_condition TEXT)")
    rows = [
        # (year, mmdd, track, kaiji, nichiji, race_num, horse_num, order)
        ("2025", "0601", "05", "01", "01", "05", "3", 1),   # ← 対象レース自身
        ("2025", "0601", "05", "01", "01", "09", "3", 2),   # ← 同じ日の別レース
        ("2025", "0301", "05", "01", "01", "07", "3", 4),   # ← 正当な過去走
        ("2024", "1201", "06", "05", "02", "11", "3", 3),   # ← 正当な過去走
    ]
    for (y, md, tr, ka, ni, rn, hn, order) in rows:
        conn.execute(
            "INSERT INTO horse_races VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (y, md, tr, ka, ni, rn, hn, BRN, order, 960, 350, 1, 25, "1", 550, "480", "+2"))
        conn.execute("INSERT INTO races VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (y, md, tr, ka, ni, rn, 1600, "11", "", "", 12, "1", "1", "0"))
    return conn


def _key(y, md, tr, ka, ni, rn) -> str:
    return f"{y}{md}{tr}{ka}{ni}{rn}"


def test_past_runs_excludes_target_race_and_same_day():
    """対象レース自身も同日の別レースも過去走に含めない (厳密に < )。"""
    conn = _pit_conn()
    runs = model._past_runs(conn, BRN, TARGET_DATE, cache={})
    keys = {model._race_key(r) for r in runs}

    assert _key("2025", "0601", "05", "01", "01", "05") not in keys, "対象レース自身が混入"
    assert _key("2025", "0601", "05", "01", "01", "09") not in keys, "同日の別レースが混入"
    assert keys == {_key("2025", "0301", "05", "01", "01", "07"),
                    _key("2024", "1201", "06", "05", "02", "11")}


def test_corner_and_prize_enrichment_never_touch_target_race():
    """索引に対象レースの値があっても、過去走集計には現れない。"""
    conn = _pit_conn()
    target_key = _key("2025", "0601", "05", "01", "01", "05")
    past_key = _key("2025", "0301", "05", "01", "01", "07")

    # 索引には「対象レース」と「過去走」の両方の値を入れておく。
    model._CORNER_INDEX = {
        target_key: {"4": {"3": 1}},        # 対象レースでは4角1番手だった (未来情報)
        past_key: {"4": {"3": 9}},          # 過去走では4角9番手
    }
    model._PRIZE_INDEX = {
        target_key: {"3": {"honsho_yen": 50_000_000}},   # 対象レースの賞金 (未来情報)
        past_key: {"3": {"honsho_yen": 1_000_000}},
    }
    try:
        runs = model._past_runs(conn, BRN, TARGET_DATE, cache={})
        corners = [r.get("_corner_last") for r in runs if r.get("_corner_last") is not None]
        prizes = [r.get("_prize_yen") for r in runs if r.get("_prize_yen") is not None]

        assert 1.0 not in corners, "対象レースの4角順位が混入"
        assert 50_000_000 not in prizes, "対象レースの賞金が混入"
        assert 9.0 in corners and 1_000_000 in prizes, "過去走の値は引けている"
    finally:
        model._CORNER_INDEX = None
        model._PRIZE_INDEX = None


def test_aggregate_over_past_runs_is_pit_safe():
    """集計特徴 (可変集計) も対象レースの結果を含まない。"""
    conn = _pit_conn()
    target_key = _key("2025", "0601", "05", "01", "01", "05")
    model._PRIZE_INDEX = {target_key: {"3": {"honsho_yen": 999_999_999}}}
    try:
        runs = model._past_runs(conn, BRN, TARGET_DATE, cache={})
        race = {"track_type_code": "11", "track_code": "05", "distance": 1600}
        got = model._aggregate_value(model.FEATURES["agg_prize"], runs, race, [], None)
        # 索引に対象レースの巨額賞金しか無い → 過去走側は空なので None になるべき
        assert got is None or got < 999_999_999
    finally:
        model._PRIZE_INDEX = None


@pytest.mark.skipif(
    not config.KEIBA_DB_PATH.exists()
    or importlib.util.find_spec("lightgbm") is None,
    reason="実 DB / lightgbm が無い環境ではスキップ")
def test_real_db_past_runs_are_strictly_before():
    """実 DB でも過去走の日付が対象日より厳密に前であること。"""
    from scripts.backtest import horses_for_race, list_races  # type: ignore
    from builder.keiba_bridge import open_conn
    with open_conn() as conn:
        races = list_races(conn, "20250601", "20250607", jra_only=True, require_confirmed=True)
        if not races:
            pytest.skip("対象期間にレースが無い")
        race = races[0]
        before = f"{race['race_year']}{race['race_month_day']}"
        checked = 0
        for h in horses_for_race(conn, race)[:6]:
            brn = h.get("blood_register_num")
            if not brn:
                continue
            for r in model._past_runs(conn, brn, before, cache={}):
                d = f"{r['race_year']}{r['race_month_day']}"
                assert d < before, f"PIT違反: 過去走 {d} >= 対象日 {before}"
                checked += 1
        assert checked > 0
