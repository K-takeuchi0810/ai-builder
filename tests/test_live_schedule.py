from __future__ import annotations

from datetime import datetime
import sqlite3

from builder import live_schedule as ls


def _conn(*, coverage: bool = True):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE schedules (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT);
      CREATE TABLE races (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,start_time TEXT);
      CREATE TABLE horse_races (
        race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,nichiji TEXT,
        race_num TEXT,horse_num TEXT,confirmed_order INTEGER);
    """)
    if coverage:
        conn.executemany("INSERT INTO schedules VALUES (?,?,?,?,?)", [
            ("2026", "0816", "01", "01", "01"),
            ("2026", "0822", "01", "01", "01"),
        ])
    return conn


def test_non_race_day_inside_official_coverage_is_disabled():
    got = ls.decide(_conn(), "20260818", now=datetime(2026, 8, 18, 7, 0))
    assert got["enabled"] is False
    assert got["reason"] == "no_official_race"


def test_race_day_uses_actual_first_and_last_start_times():
    conn = _conn()
    conn.execute("INSERT INTO schedules VALUES ('2026','0822','04','01','01')")
    conn.executemany("INSERT INTO races VALUES (?,?,?,?,?,?,?)", [
        ("2026", "0822", "04", "01", "01", "01", "0940"),
        ("2026", "0822", "04", "01", "01", "12", "1830"),
    ])
    got = ls.decide(conn, "20260822", now=datetime(2026, 8, 22, 0, 5))
    assert got["enabled"] is True and got["reason"] == "race_times"
    assert got["start_at"].startswith("2026-08-22T07:40")
    assert got["end_at"].startswith("2026-08-22T20:30")


def test_official_race_day_without_race_rows_uses_safe_window():
    got = ls.decide(_conn(), "20260822", now=datetime(2026, 8, 22, 0, 5))
    assert got["enabled"] is True
    assert got["reason"] == "official_schedule_waiting_for_races"
    assert got["start_at"].startswith("2026-08-22T07:00")
    assert got["end_at"].startswith("2026-08-22T21:00")


def test_unknown_schedule_coverage_fails_safe_instead_of_silently_disabling():
    got = ls.decide(_conn(coverage=False), "20260822",
                    now=datetime(2026, 8, 22, 0, 5))
    assert got["enabled"] is True
    assert got["reason"] == "schedule_coverage_unknown"


def test_all_results_confirmed_disables_remaining_runs():
    conn = _conn()
    conn.execute("INSERT INTO races VALUES ('2026','0822','04','01','01','01','0940')")
    conn.execute(
        "INSERT INTO horse_races VALUES ('2026','0822','04','01','01','01','01',1)"
    )
    got = ls.decide(conn, "20260822", now=datetime(2026, 8, 22, 10, 0))
    assert got["enabled"] is False
    assert got["reason"] == "all_results_confirmed"


def test_changed_late_start_extends_window_from_database():
    conn = _conn()
    conn.execute("INSERT INTO races VALUES ('2026','0822','04','01','01','12','1945')")
    got = ls.decide(conn, "20260822", now=datetime(2026, 8, 22, 18, 5))
    assert got["enabled"] is True
    assert got["end_at"].startswith("2026-08-22T21:45")
