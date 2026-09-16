"""Decide when the one-minute JV-Link live task should be active."""

from __future__ import annotations

from datetime import datetime, timedelta


PRE_RACE_MINUTES = 120
POST_RACE_MINUTES = 120
FALLBACK_START_HOUR = 7
FALLBACK_END_HOUR = 21


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _valid_start(value: object) -> str | None:
    raw = str(value or "").strip()[:4]
    if len(raw) != 4 or not raw.isdigit():
        return None
    hour, minute = int(raw[:2]), int(raw[2:])
    return raw if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _at(target: str, hhmm: str) -> datetime:
    return datetime.strptime(target + hhmm, "%Y%m%d%H%M")


def decide(conn, target: str, *, now: datetime | None = None) -> dict:
    """Return an activation window based on official schedules and race rows.

    Absence is trusted only when ``target`` lies inside the downloaded YS
    schedule coverage.  Outside that coverage the conservative fallback window
    is enabled so a stale schedule database cannot silently suppress updates.
    """
    current = now or datetime.now()
    if len(target) != 8 or not target.isdigit():
        raise ValueError("target must be YYYYMMDD")

    schedule_count = 0
    coverage_min = coverage_max = None
    if _table_exists(conn, "schedules"):
        coverage = conn.execute(
            "SELECT MIN(race_year||race_month_day),MAX(race_year||race_month_day) "
            "FROM schedules WHERE track_code BETWEEN '01' AND '10'"
        ).fetchone()
        if coverage:
            coverage_min, coverage_max = coverage[0], coverage[1]
        schedule_count = conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE race_year||race_month_day=? "
            "AND track_code BETWEEN '01' AND '10'", (target,)
        ).fetchone()[0]

    races: list[dict] = []
    if _table_exists(conn, "races"):
        result_sql = (
            "EXISTS(SELECT 1 FROM horse_races hr WHERE "
            "hr.race_year=r.race_year AND hr.race_month_day=r.race_month_day "
            "AND hr.track_code=r.track_code AND hr.kaiji=r.kaiji "
            "AND hr.nichiji=r.nichiji AND hr.race_num=r.race_num "
            "AND CAST(COALESCE(hr.confirmed_order,'0') AS INTEGER)>0)"
            if _table_exists(conn, "horse_races") else "0"
        )
        rows = conn.execute(
            "SELECT r.start_time," + result_sql + " result_confirmed "
            "FROM races r WHERE r.race_year||r.race_month_day=? "
            "AND r.track_code BETWEEN '01' AND '10'", (target,)
        ).fetchall()
        races = [{"start_time": row[0], "result_confirmed": bool(row[1])}
                 for row in rows]

    valid_times = [_valid_start(race["start_time"]) for race in races]
    valid_times = [value for value in valid_times if value]
    if races and valid_times and all(race["result_confirmed"] for race in races):
        return _disabled(target, "all_results_confirmed", schedule_count,
                         coverage_min, coverage_max, len(races))

    if valid_times:
        start = min(_at(target, value) for value in valid_times) - timedelta(
            minutes=PRE_RACE_MINUTES
        )
        end = max(_at(target, value) for value in valid_times) + timedelta(
            minutes=POST_RACE_MINUTES
        )
        reason = "race_times"
    elif schedule_count:
        start = _at(target, f"{FALLBACK_START_HOUR:02d}00")
        end = _at(target, f"{FALLBACK_END_HOUR:02d}00")
        reason = "official_schedule_waiting_for_races"
    elif coverage_min and coverage_max and coverage_min <= target <= coverage_max:
        return _disabled(target, "no_official_race", schedule_count,
                         coverage_min, coverage_max, len(races))
    else:
        start = _at(target, f"{FALLBACK_START_HOUR:02d}00")
        end = _at(target, f"{FALLBACK_END_HOUR:02d}00")
        reason = "schedule_coverage_unknown"

    if current >= end:
        return _disabled(target, "window_finished", schedule_count,
                         coverage_min, coverage_max, len(races))
    effective_start = max(start, current.replace(second=0, microsecond=0) + timedelta(minutes=1))
    return {
        "enabled": True,
        "target": target,
        "reason": reason,
        "start_at": effective_start.isoformat(timespec="seconds"),
        "end_at": end.isoformat(timespec="seconds"),
        "scheduled_start_at": start.isoformat(timespec="seconds"),
        "race_count": len(races),
        "schedule_count": schedule_count,
        "coverage_min": coverage_min,
        "coverage_max": coverage_max,
    }


def _disabled(target: str, reason: str, schedule_count: int,
              coverage_min: str | None, coverage_max: str | None,
              race_count: int) -> dict:
    return {
        "enabled": False, "target": target, "reason": reason,
        "start_at": None, "end_at": None, "scheduled_start_at": None,
        "race_count": race_count, "schedule_count": schedule_count,
        "coverage_min": coverage_min, "coverage_max": coverage_max,
    }
