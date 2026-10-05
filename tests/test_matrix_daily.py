"""matrix_daily (当日レースの日次バッチ) のテスト。

DB 層はモックして高速・CIセーフに保つ。実 DB 経路は skip gate 付きで1本だけ。
"""

from __future__ import annotations

import contextlib
from datetime import datetime
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, matrix, matrix_daily as md, model   # noqa: E402

SPECS = [{"key": "popularity"},
         {"key": "agg_avg_finish", "lookback": 5, "match": []}]


def _fake_race(race_num="01", weight="480"):
    return {"race_year": "2026", "race_month_day": "0801", "track_code": "04",
            "kaiji": "03", "nichiji": "05", "race_num": race_num, "distance": 1800,
            "track_type_code": "11", "turf_condition": "1", "dirt_condition": "0",
            "weather_code": "1", "race_name": f"テスト{race_num}R",
            "start_time": "1545"}


def _fake_horses(n=8, weight="480", with_order=False):
    out = []
    for k in range(1, n + 1):
        out.append({"horse_num": f"{k:02d}", "blood_register_num": f"20201000{k:02d}",
                    "horse_name": f"テストホース{k}", "win_odds": 20 + k * 10,
                    "win_popularity": k, "horse_weight": weight,
                    "confirmed_order": (k if with_order else 0)})
    return out


def _patch_db(monkeypatch, races, horses, past_runs=None):
    """keiba-yosou の DB 依存をすべてモックする。"""
    monkeypatch.setattr(md, "open_conn", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(md, "_ensure_keiba_on_path", lambda: None)

    fake = type(sys)("scripts.backtest")
    fake.list_races = lambda conn, lo, hi, jra_only=True, require_confirmed=False: races
    fake.horses_for_race = lambda conn, race: horses
    fake.get_payout_row = lambda conn, race: None
    fake.popularity_config = lambda: {"max_snapshot_age_min": 30}
    fake.race_odds_untrusted = lambda h, r, a: False
    scripts = type(sys)("scripts")
    scripts.backtest = fake
    monkeypatch.setitem(sys.modules, "scripts", scripts)
    monkeypatch.setitem(sys.modules, "scripts.backtest", fake)
    monkeypatch.setattr(model, "_past_runs",
                        lambda conn, brn, before, limit=50, cache=None: past_runs or [])


def test_build_daily_uses_entry_list_without_results(tmp_path, monkeypatch):
    """当日は結果が無い (confirmed_order=0) 状態でも基底列を計算できること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))

    daily = md.build_daily("20260801", SPECS)
    assert daily["date"] == "20260801"
    assert len(daily["races"]) == 1
    r = daily["races"][0]
    assert r["race_id"] == "202608010403050 1".replace(" ", "")   # 年月日場回日R
    assert len(r["horses"]) == 8
    h = r["horses"][0]
    assert h["order"] == 0                        # 結果はまだ無い
    assert h["odds"] == 3.0                       # win_odds 30 → 3.0倍
    assert h["name"] == "テストホース1"
    assert h["n_past_runs"] == 0                   # カバレッジ表示用
    assert set(h["x"]) == {c["id"] for c in daily["columns"]}
    assert r["weight_announced"] is True           # horse_weight が数字


def test_race_name_falls_back_to_conditions(tmp_path, monkeypatch):
    """平場は race_name が空 (重賞のみ命名) なので条件から表示名を作ること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    race = _fake_race()
    race["race_name"] = ""                      # 実データの平場と同じ状態
    _patch_db(monkeypatch, [race], _fake_horses(8))
    daily = md.build_daily("20260801", SPECS)
    name = daily["races"][0]["race_name"]
    assert name and name.strip()                # 空文字を UI に出さない
    assert "1800m" in name                      # 距離が入る
    assert "芝" in name                          # 芝ダートが入る

    # 重賞など名前があるレースはそのまま使う
    named = _fake_race()
    named["race_name"] = "テスト記念"
    _patch_db(monkeypatch, [named], _fake_horses(8))
    daily2 = md.build_daily("20260802", SPECS)
    assert daily2["races"][0]["race_name"] == "テスト記念"


def test_weight_not_announced_is_detected(tmp_path, monkeypatch):
    """馬体重未発表なら weight_announced=False (設計書 §3.3 の「分析待ち」判定)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8, weight="   "))
    daily = md.build_daily("20260801", SPECS)
    assert daily["races"][0]["weight_announced"] is False


def test_daily_cache_roundtrip_and_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    calls = []
    races = [_fake_race()]

    def counting_list_races(conn, lo, hi, jra_only=True, require_confirmed=False):
        calls.append(lo)
        return races

    _patch_db(monkeypatch, races, _fake_horses(6))
    sys.modules["scripts.backtest"].list_races = counting_list_races

    md.build_daily("20260801", SPECS)
    assert len(calls) == 1
    md.build_daily("20260801", SPECS)                 # キャッシュから
    assert len(calls) == 1
    assert md.load_daily("20260801", SPECS)["date"] == "20260801"
    md.build_daily("20260801", SPECS, rebuild=True)    # 強制再計算
    assert len(calls) == 2
    # 原子的書き込み: .tmp が残らない
    assert not list((tmp_path / "daily").glob("*.tmp"))


def test_find_race_and_today_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race("01"), _fake_race("02")], _fake_horses(8))
    daily = md.build_daily("20260801", SPECS)

    rid = daily["races"][0]["race_id"]
    assert md.find_race(daily, rid)["race_num"] == "01"
    assert md.find_race(daily, "nope") is None

    status = md.today_status(daily)
    assert len(status) == 2
    s = status[0]
    assert s["race_id"] == rid and s["n_horses"] == 8
    assert s["weight_announced"] is True
    # popularity は全馬充填なのでゲート通過、agg_avg_finish は過去走0件で欠損 → 未通過
    assert "agg_avg_finish|lb=5|m=" in s["gate_missing_columns"]
    assert s["n_gate_missing"] == 1 and s["n_gate_passed"] == 1
    assert s["gate_pass_rate"] == 0.5
    # 一部の列が欠けても「予想は可能」なので ready は True
    # (実測: 直近レースでも 425列中 中央値5列は欠ける。全列通過を要求するのは誤り)
    assert s["ready"] is True


def test_today_status_ready_when_all_columns_pass(tmp_path, monkeypatch):
    """全列がゲートを通れば未通過0・通過率1.0。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    daily = md.build_daily("20260801", [{"key": "popularity"}])
    status = md.today_status(daily)
    assert status[0]["gate_missing_columns"] == []
    assert status[0]["gate_pass_rate"] == 1.0
    assert status[0]["ready"] is True


def test_today_status_not_ready_when_nothing_passes(tmp_path, monkeypatch):
    """1列も通らないレースは ready=False (予想が成立しない)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    # 過去走ゼロ → 集計列のみの構成では 1 列も通らない
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    daily = md.build_daily("20260801", [{"key": "agg_avg_finish", "lookback": 5, "match": []}])
    status = md.today_status(daily)
    assert status[0]["n_gate_passed"] == 0
    assert status[0]["gate_pass_rate"] == 0.0
    assert status[0]["ready"] is False


def test_start_time_is_formatted_for_display(tmp_path, monkeypatch):
    """発走時刻は "1545" → "15:45"。欠損は None (UI に "--:--" を出させる)。"""
    assert md._hhmm("1545") == "15:45"
    assert md._hhmm(1545) == "15:45"
    assert md._hhmm("") is None and md._hhmm(None) is None
    assert md._hhmm("15") is None            # 桁が足りない
    assert md._hhmm("abcd") is None          # 数字でない

    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    daily = md.build_daily("20260801", SPECS)
    assert daily["races"][0]["start_time"] == "15:45"
    status = md.today_status(daily)[0]
    assert status["start_time"] == "15:45"
    assert status["date"] == "20260801"


def test_race_started_uses_server_time_and_today_status_exposes_it(tmp_path, monkeypatch):
    before = datetime(2026, 8, 2, 9, 59)
    at_start = datetime(2026, 8, 2, 10, 0)
    race = {"date": "20260802", "start_time": "10:00"}
    assert md.race_started(race, now=before) is False
    assert md.race_started(race, now=at_start) is True

    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    raw = _fake_race()
    raw["race_month_day"] = "0802"
    raw["start_time"] = "1000"
    _patch_db(monkeypatch, [raw], _fake_horses(8))
    daily = md.build_daily("20260802", SPECS)
    assert md.today_status(daily, now=before)[0]["started"] is False
    assert md.today_status(daily, now=at_start)[0]["started"] is True


def test_odds_as_of_comes_from_db_not_wall_clock(tmp_path, monkeypatch):
    """オッズ取得時刻は DB の odds_fetched_at のみ。全馬 NULL なら None を返す。

    UI がクライアントの時計で「○○時点」を捏造しないための土台 (UI指示書 §4)。
    """
    assert md._odds_as_of([]) is None
    assert md._odds_as_of([{"odds_fetched_at": None}]) is None
    # ISO 文字列なので辞書順の max が時刻順の max と一致する
    assert md._odds_as_of([
        {"odds_fetched_at": "2026-08-01T14:02:00"},
        {"odds_fetched_at": "2026-08-01T15:31:07"},
        {"odds_fetched_at": None},
    ]) == "2026-08-01T15:31:07"

    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    horses = _fake_horses(4)
    for h in horses:
        h["odds_fetched_at"] = "2026-08-01T15:31:07"
    _patch_db(monkeypatch, [_fake_race()], horses)
    daily = md.build_daily("20260801", SPECS)
    assert daily["races"][0]["odds_as_of"] == "2026-08-01T15:31:07"
    assert md.today_status(daily)[0]["odds_as_of"] == "2026-08-01T15:31:07"


def test_latest_weather_only_update_does_not_erase_announced_going():
    """天候だけのWE更新（馬場0）より前にある発表済み馬場を引き継ぐ。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE weather_going (
        race_year TEXT, race_month_day TEXT, track_code TEXT, kaiji TEXT,
        nichiji TEXT, announced_time TEXT, weather_code TEXT,
        going_turf TEXT, going_dirt TEXT)""")
    conn.executemany("INSERT INTO weather_going VALUES (?,?,?,?,?,?,?,?,?)", [
        ("2026", "0816", "07", "02", "08", "00000000", "1", "1", "1"),
        # 実データと同じく、後続行は天候だけ変わり馬場欄が0。
        ("2026", "0816", "07", "02", "08", "08160655", "2", "0", "0"),
        ("2026", "0816", "07", "02", "08", "08160851", "1", "0", "0"),
    ])
    race = {"race_year": "2026", "race_month_day": "0816", "track_code": "07",
            "kaiji": "02", "nichiji": "08", "weather_code": "0",
            "turf_condition": "0", "dirt_condition": "0"}
    got = md._with_latest_weather(conn, race)
    assert got["weather_code"] == "1"
    assert got["turf_condition"] == "1" and got["dirt_condition"] == "1"
    assert got["condition_as_of"] == "00000000"


def test_effective_race_changes_expose_metadata_and_change_feature_context():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE start_time_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,announced_time TEXT,new_start_time TEXT,old_start_time TEXT);
      CREATE TABLE course_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,announced_time TEXT,new_distance INTEGER,new_track_type_code TEXT,
        old_distance INTEGER,old_track_type_code TEXT,reason_code TEXT);
    """)
    key = ("2026", "0801", "04", "03", "05", "01")
    conn.execute("INSERT INTO start_time_changes VALUES (?,?,?,?,?,?,?,?,?)",
                 (*key, "08010900", "1550", "1545"))
    conn.execute("INSERT INTO course_changes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (*key, "08010901", 1600, "23", 1800, "11", "1"))

    changed = md._with_latest_race_changes(conn, _fake_race())
    assert changed["start_time"] == "1550"
    assert changed["original_start_time"] == "1545"
    assert changed["distance"] == 1600 and changed["track_type_code"] == "23"
    assert changed["course_change"]["old_surface_label"] == "芝"
    assert changed["course_change"]["new_surface_label"] == "ダート"

    horses = _fake_horses(2)
    before = md._feature_context(matrix._seg(_fake_race()), horses)
    after = md._feature_context(matrix._seg(changed), horses)
    assert before != after


def test_today_status_calls_unannounced_going_waiting_not_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    race = _fake_race()
    race["turf_condition"] = "0"
    _patch_db(monkeypatch, [race], _fake_horses(8))
    status = md.today_status(md.build_daily("20260801", SPECS))[0]
    assert status["ready"] is True
    assert status["condition_announced"] is False
    assert status["condition_label"] == "馬場発表待ち"


def test_refresh_live_merges_odds_weight_and_current_features(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    initial = _fake_horses(4, weight="")
    for horse in initial:
        horse["win_odds"] = 0
        horse["win_popularity"] = 0
    _patch_db(monkeypatch, [_fake_race()], initial)
    daily = md.build_daily("20260801", SPECS)
    before_revision = daily["races"][0]["live_revision"]

    live = _fake_horses(4, weight="486")
    for index, horse in enumerate(live, 1):
        horse["win_odds"] = 30 + index
        horse["win_popularity"] = index
        horse["odds_fetched_at"] = "2026-08-01T14:31:00"
        horse["burden_weight"] = 555
        horse["weight_change_sign"] = "+"
        horse["weight_change_diff"] = "4"
    sys.modules["scripts.backtest"].horses_for_race = lambda conn, race: live

    refreshed = md.refresh_live(daily)
    race = refreshed["races"][0]
    horse = race["horses"][0]
    assert daily["races"][0]["weight_announced"] is False  # atomic copy; old readers are safe
    assert race["weight_announced"] is True
    assert race["odds_as_of"] == "2026-08-01T14:31:00"
    assert race["live_revision"] != before_revision
    assert race["live_updated_at"]
    assert horse["odds"] == 3.1 and horse["pop"] == 1
    assert horse["horse_weight"] == 486 and horse["horse_weight_change"] == 4
    assert horse["burden_weight"] == 55.5
    assert horse["x"]["popularity"] == 1
    status = md.today_status(refreshed)[0]
    assert status["live_revision"] == race["live_revision"]
    assert status["live_updated_at"] == race["live_updated_at"]


def test_refresh_live_recomputes_going_and_jockey_and_excludes_scratch(monkeypatch):
    """馬場・騎手変更は採点を更新し、AV馬は正規化母集団から除外する。"""
    race = _fake_race()
    race["turf_condition"] = "2"
    live = _fake_horses(2)
    live[0].update({"jockey_code": "NEW01", "jockey_short_name": "新騎手"})
    live[1].update({"jockey_code": "KEEP2", "jockey_short_name": "騎手2"})
    _patch_db(monkeypatch, [race], live)
    monkeypatch.setattr(md, "_inactive_horses", lambda conn, r: {
        "02": {"label": "出走取消", "announced_time": "08011000",
               "horse_name": "テストホース2"}
    })
    calls = []

    def compute(conn, horse, current_race, cache):
        calls.append((horse["horse_num"], current_race["turf_condition"]))
        return {
            "jockey_win_rate": 0.42 if horse["jockey_code"] == "NEW01" else 0.1,
            "same_going_runs": 4,
            "same_going_top3": 3 if current_race["turf_condition"] == "2" else 0,
        }

    monkeypatch.setattr(model, "_compute_features", compute)
    cols = matrix._columns([
        {"key": "popularity"}, {"key": "jockey_win_rate"}, {"key": "fit_going"},
    ])
    daily = {
        "version": md.DAILY_VERSION, "date": "20260801", "columns": cols,
        "races": [{
            "race_id": md._race_id(race), "date": "20260801", "race_num": "01",
            "race_name": "テスト", "start_time": "15:45",
            "seg": {"track": "04", "surface": "turf", "distance": 1800,
                    "condition": "firm"},
            "horses": [
                {"num": "01", "name": "テストホース1", "x": {
                    "popularity": 1, "jockey_win_rate": 0.01, "fit_going": 0.0}},
                {"num": "02", "name": "テストホース2", "x": {
                    "popularity": 2, "jockey_win_rate": 0.02, "fit_going": 0.0}},
            ],
            "tan": {}, "trusted": True, "weight_announced": True,
            "live_feature_context": "old-context",
        }],
    }
    got = md.refresh_live(daily)
    out = got["races"][0]
    assert calls == [("01", "2")]  # 取消馬は再採点しない
    assert out["horses"][0]["jockey"] == "新騎手"
    assert out["horses"][0]["x"]["jockey_win_rate"] == 0.42
    assert out["horses"][0]["x"]["fit_going"] == 0.75
    assert out["horses"][1]["scratched"] is True
    assert out["scratched_horses"][0]["label"] == "出走取消"
    prepared = matrix.prepare_races(got)[0]
    assert set(prepared["z"]) == {"01"}
    status = md.today_status(got)[0]
    assert status["n_horses"] == 1 and status["n_scratched"] == 1


def test_today_status_carries_display_fields(tmp_path, monkeypatch):
    """一覧表示に必要な条件と「結果待ち」判定を返すこと。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    s = md.today_status(md.build_daily("20260801", SPECS))[0]
    assert s["surface"] == "turf" and s["distance"] == 1800
    assert s["condition"] is not None
    assert s["finished"] is False              # 当日は confirmed_order=0

    # 確定後は finished=True (1着馬がいる)
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8, with_order=True))
    s2 = md.today_status(md.build_daily("20260802", SPECS))[0]
    assert s2["finished"] is True


def test_daily_version_is_in_cache_filename(tmp_path, monkeypatch):
    """形が変わったら旧キャッシュを掴まないこと (version をファイル名に含める)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    p = md._daily_path("20260801", matrix._columns(SPECS))
    assert f"daily_v{md.DAILY_VERSION}_20260801_" in p.name


def test_historical_daily_falls_back_to_latest_compatible_version(tmp_path, monkeypatch):
    """版更新前の開催結果を購入履歴から消さないこと。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    directory = tmp_path / "daily"
    directory.mkdir()
    columns_hash = matrix._col_hash(matrix._columns(SPECS))
    old = {"version": md.DAILY_VERSION - 2, "date": "20260801", "races": [{"race_id": "R4"}]}
    newer = {"version": md.DAILY_VERSION - 1, "date": "20260801", "races": [{"race_id": "R5"}]}
    (directory / f"daily_v{md.DAILY_VERSION - 2}_20260801_{columns_hash}.json").write_text(
        json.dumps(old), encoding="utf-8")
    (directory / f"daily_v{md.DAILY_VERSION - 1}_20260801_{columns_hash}.json").write_text(
        json.dumps(newer), encoding="utf-8")
    # 列構成が異なるファイルは、新しくても選ばない。
    (directory / f"daily_v{md.DAILY_VERSION - 1}_20260801_other.json").write_text(
        json.dumps({"date": "20260801", "races": [{"race_id": "WRONG"}]}),
        encoding="utf-8")

    got = md.load_historical_daily("20260801", SPECS)
    assert got["races"][0]["race_id"] == "R5"


def test_historical_daily_prefers_current_version(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    directory = tmp_path / "daily"
    directory.mkdir()
    columns_hash = matrix._col_hash(matrix._columns(SPECS))
    current = {"version": md.DAILY_VERSION, "date": "20260801",
               "races": [{"race_id": "CURRENT"}]}
    (directory / f"daily_v{md.DAILY_VERSION}_20260801_{columns_hash}.json").write_text(
        json.dumps(current), encoding="utf-8")
    assert md.load_historical_daily("20260801", SPECS) == current


def test_output_shape_is_compatible_with_matrix_helpers(tmp_path, monkeypatch):
    """日次出力が prepare_races / score_race_detailed でそのまま使えること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(10))
    daily = md.build_daily("20260801", [{"key": "popularity"}])

    prep = matrix.prepare_races(daily)
    assert len(prep) == 1 and prep[0]["race_id"] == daily["races"][0]["race_id"]
    assert prep[0]["index"] == 0
    # 人気 (小さいほど良い) の z が入っている
    assert prep[0]["z"]["01"]["popularity"] > prep[0]["z"]["10"]["popularity"]


@pytest.mark.skipif(
    not config.KEIBA_DB_PATH.exists() or importlib.util.find_spec("lightgbm") is None,
    reason="実 DB / lightgbm が無い環境ではスキップ")
def test_real_db_daily_build_smoke(tmp_path, monkeypatch):
    """実 DB で確定済みの1日を日次ビルドできること (PIT: 過去走は前日まで)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    daily = md.build_daily("20250105", SPECS, require_confirmed=True)
    if not daily["races"]:
        pytest.skip("対象日にレースが無い")
    r = daily["races"][0]
    assert r["date"] == "20250105" and r["horses"]
    for h in r["horses"]:
        assert set(h["x"]) == {c["id"] for c in daily["columns"]}
    st = md.today_status(daily)
    assert len(st) == len(daily["races"])


# ---------------------------------------------------------------------------
# 当日特徴量の再計算が「なぜ回ったか」を残す
# ---------------------------------------------------------------------------
# 1レースの再計算は実測で 8〜12 秒・約400本のDB問い合わせ。24レースで約250秒かかる。
# ライブ更新は25秒間隔なので、毎周回ると**実質ずっと回り続ける**。
# 頻度と理由が分からないと、妥当な費用かどうかを判断できない。
def _parts(**over):
    got = {"condition": "良", "track": "05", "surface": "芝", "distance": 1600,
           "jockeys": [["01", "J1", "55"], ["02", "J2", "54"]]}
    got.update(over)
    return got


def test_the_context_fingerprint_is_stable_for_the_same_input():
    """同じ入力なら指紋が変わらないこと。

    ここが揺れると、変わっていないのに毎周再計算が走る。
    """
    seg = {"condition": "良", "track": "05", "surface": "芝", "distance": 1600}
    horses = [{"horse_num": "1", "jockey_code": "J1", "burden_weight": 55},
              {"horse_num": "2", "jockey_code": "J2", "burden_weight": 54}]
    first = md._feature_context(seg, horses)
    assert first == md._feature_context(seg, horses)
    # 並び順が違っても同じ (sorted しているため)
    assert first == md._feature_context(seg, list(reversed(horses)))


def test_the_context_changes_only_for_the_inputs_it_declares():
    """馬場・騎手・負担重量が変われば指紋も変わること。"""
    seg = {"condition": "良", "track": "05", "surface": "芝", "distance": 1600}
    horses = [{"horse_num": "1", "jockey_code": "J1", "burden_weight": 55}]
    base = md._feature_context(seg, horses)
    assert md._feature_context({**seg, "condition": "稍重"}, horses) != base
    assert md._feature_context(
        seg, [{**horses[0], "jockey_code": "J9"}]) != base
    assert md._feature_context(
        seg, [{**horses[0], "burden_weight": 57}]) != base
    # オッズや馬体重は指紋に入っていない (入れると毎周回ってしまう)
    assert md._feature_context(
        seg, [{**horses[0], "odds": 3.5, "horse_weight": 480}]) == base


def test_the_reason_for_a_recompute_is_reported():
    """何がきっかけで再計算したのかを項目名で返すこと。"""
    base = _parts()
    assert md._context_changes(base, _parts(condition="稍重")) == ["condition:良→稍重"]
    assert md._context_changes(
        base, _parts(jockeys=[["01", "J9", "55"], ["02", "J2", "54"]])) \
        == ["jockeys:1頭ぶん"]
    # 複数同時
    got = md._context_changes(
        base, _parts(condition="重", jockeys=[["01", "J9", "55"]]))
    assert "condition:良→重" in got and any("jockeys" in x for x in got)


def test_a_first_run_says_so_instead_of_inventing_a_cause():
    """前回の記録が無いときは「差分」を捏造しないこと。"""
    assert md._context_changes(None, _parts()) == ["(前回の記録なし)"]
    assert md._context_changes({}, _parts()) == ["(前回の記録なし)"]


def test_an_unchanged_context_is_not_reported_as_a_diff():
    """指紋が同じなら差分は出ない (そもそも再計算されない)。"""
    base = _parts()
    assert md._context_changes(base, base) == ["(項目の差は検出できず)"]
