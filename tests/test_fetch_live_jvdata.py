from __future__ import annotations

from datetime import datetime
import contextlib
import importlib.util
from pathlib import Path
import sqlite3

AV_LENGTH = 78
JC_LENGTH = 161
TC_LENGTH = 45
CC_LENGTH = 50


def _module():
    path = Path("deploy/windows/fetch-live-jvdata.py")
    spec = importlib.util.spec_from_file_location("maib_fetch_live_jvdata", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _race(**overrides):
    race = {
        "race_year": "2026",
        "race_month_day": "0802",
        "track_code": "07",
        "kaiji": "01",
        "nichiji": "01",
        "race_num": "03",
        "start_time": "1040",
        "result_confirmed": 0,
    }
    race.update(overrides)
    return race


def test_result_key_uses_jvlink_0b12_short_key():
    mod = _module()
    assert mod._result_key(_race()) == "202608020703"


def test_result_due_only_returns_finished_unconfirmed_race():
    mod = _module()
    now = datetime(2026, 8, 2, 10, 45)
    assert mod._result_due(_race(), now) is True
    assert mod._result_due(_race(result_confirmed=1), now) is False
    assert mod._result_due(_race(start_time="1044"), now) is False


def test_scheduled_fetch_runs_all_day_and_activity_window_follows_races():
    mod = _module()
    rows = [_race(start_time="0940"), _race(start_time="1830", race_num="12")]
    assert mod._day_update_due(rows, datetime(2026, 8, 2, 7, 39)) is False
    assert mod._day_update_due(rows, datetime(2026, 8, 2, 7, 40)) is True
    assert mod._day_update_due(rows, datetime(2026, 8, 2, 18, 30)) is True
    assert mod._day_update_due(rows, datetime(2026, 8, 2, 18, 31)) is False
    assert mod._day_update_due([_race(result_confirmed=1)],
                               datetime(2026, 8, 2, 10, 0)) is False

    script = Path("deploy/windows/install-live-jvdata-task.ps1").read_text(encoding="utf-8")
    controller = Path("deploy/windows/Configure-LiveJvdataTask.ps1").read_text(encoding="utf-8")
    assert "New-TimeSpan -Hours 6" in script
    assert "New-TimeSpan -Minutes 1" in controller
    assert "race days and race-time window only" in script
    cmd = Path("deploy/windows/fetch-live-jvdata.cmd").read_text(encoding="utf-8")
    assert "Start-Sleep -Seconds 30" in cmd and "--critical" in cmd


def _put(buf: bytearray, pos: int, value: str) -> None:
    raw = value.encode("cp932")
    buf[pos - 1:pos - 1 + len(raw)] = raw


def test_0b14_snapshot_reverts_withdrawn_jockey_change_and_cancellation(tmp_path, monkeypatch):
    mod = _module()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE jockey_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT,old_burden_weight INTEGER,old_jockey_code TEXT,
        old_jockey_name TEXT,old_apprentice_code TEXT);
      CREATE TABLE race_cancellations (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT);
      CREATE TABLE horse_races (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT,burden_weight INTEGER,jockey_code TEXT,
        jockey_short_name TEXT,jockey_apprentice_code TEXT);
    """)
    key = ("2026", "0802", "07", "01", "01", "03", "01")
    conn.execute("INSERT INTO jockey_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (*key, 550, "OLD01", "旧騎手", "0"))
    conn.execute("INSERT INTO horse_races VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (*key, 555, "NEW01", "新騎手", "1"))
    conn.execute("INSERT INTO race_cancellations VALUES (?,?,?,?,?,?,?)",
                 ("2026", "0802", "07", "01", "01", "04", "09"))

    raw_dir = tmp_path / "data" / "raw" / "0B14"
    raw_dir.mkdir(parents=True)
    jc = bytearray(b" " * JC_LENGTH)
    _put(jc, 1, "JC"); _put(jc, 12, "2026"); _put(jc, 16, "0802")
    _put(jc, 20, "07"); _put(jc, 22, "01"); _put(jc, 24, "01")
    _put(jc, 26, "05"); _put(jc, 36, "02")  # 現在有効なのは別の変更
    av = bytearray(b" " * AV_LENGTH)
    _put(av, 1, "AV"); _put(av, 12, "2026"); _put(av, 16, "0802")
    _put(av, 20, "07"); _put(av, 22, "01"); _put(av, 24, "01")
    _put(av, 26, "06"); _put(av, 36, "03")  # 現在有効なのは別の取消
    (raw_dir / "live.jvd").write_bytes(bytes(jc) + b"\r\n" + bytes(av) + b"\r\n")
    monkeypatch.setattr(mod, "KEIBA", tmp_path)
    monkeypatch.setattr(mod, "open_db", lambda: contextlib.nullcontext(conn))

    stats = mod.reconcile_0b14_snapshot({"live.jvd"}, "20260802")
    restored = conn.execute(
        "SELECT burden_weight,jockey_code,jockey_short_name FROM horse_races"
    ).fetchone()
    assert tuple(restored) == (550, "OLD01", "旧騎手")
    assert conn.execute("SELECT COUNT(*) FROM jockey_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM race_cancellations").fetchone()[0] == 0
    assert stats["jockey_changes_reverted"] == 1
    assert stats["cancellations_removed"] == 1
    assert stats["anomalies"] == 0


def test_0b14_snapshot_restores_withdrawn_time_course_and_weather(tmp_path, monkeypatch):
    mod = _module()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE races (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,start_time TEXT,distance INTEGER,track_type_code TEXT);
      CREATE TABLE start_time_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,announced_time TEXT,new_start_time TEXT,old_start_time TEXT);
      CREATE TABLE course_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,announced_time TEXT,new_distance INTEGER,new_track_type_code TEXT,
        old_distance INTEGER,old_track_type_code TEXT);
      CREATE TABLE weather_going (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        announced_time TEXT);
      CREATE TABLE jockey_changes (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT,old_burden_weight INTEGER,old_jockey_code TEXT,
        old_jockey_name TEXT,old_apprentice_code TEXT);
      CREATE TABLE race_cancellations (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT);
      CREATE TABLE horse_races (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT,burden_weight INTEGER,jockey_code TEXT,
        jockey_short_name TEXT,jockey_apprentice_code TEXT);
    """)
    race_key = ("2026", "0802", "07", "01", "01", "03")
    conn.execute("INSERT INTO races VALUES (?,?,?,?,?,?,?,?,?)",
                 (*race_key, "1055", 1800, "23"))
    conn.execute("INSERT INTO start_time_changes VALUES (?,?,?,?,?,?,?,?,?)",
                 (*race_key, "08020900", "1055", "1050"))
    conn.execute("INSERT INTO course_changes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (*race_key, "08020901", 1800, "23", 1600, "11"))
    conn.execute("INSERT INTO weather_going VALUES (?,?,?,?,?,?)",
                 (*race_key[:5], "08020800"))

    raw_dir = tmp_path / "data" / "raw" / "0B14"
    raw_dir.mkdir(parents=True)
    (raw_dir / "live.jvd").write_bytes(b"")
    monkeypatch.setattr(mod, "KEIBA", tmp_path)
    monkeypatch.setattr(mod, "open_db", lambda: contextlib.nullcontext(conn))

    stats = mod.reconcile_0b14_snapshot({"live.jvd"}, "20260802")
    assert tuple(conn.execute(
        "SELECT start_time,distance,track_type_code FROM races"
    ).fetchone()) == ("1050", 1600, "11")
    assert conn.execute("SELECT COUNT(*) FROM start_time_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM course_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM weather_going").fetchone()[0] == 0
    assert stats["start_times_restored"] == 1
    assert stats["courses_restored"] == 1
    assert stats["weather_changes_removed"] == 1
