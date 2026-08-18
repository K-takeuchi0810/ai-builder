"""MAIBuilder用の馬体重・馬場・全券種オッズ・確定結果をJV-Linkから更新する。

32-bit Pythonで実行する。JV-Link公式仕様の速報データだけを保存し、取得できない
値を推測しない。MAIBuilder本体は更新されたkeiba.dbをread-onlyで参照する。
"""

from __future__ import annotations

import os
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KEIBA = ROOT.parent / "keiba-yosou"
sys.path.insert(0, str(KEIBA))
sys.path.insert(0, str(ROOT))

from db import open_db  # type: ignore  # noqa: E402
from jvlink_client import JVLinkClient  # type: ignore  # noqa: E402
from jvlink_client.ingest import _split_records, ingest_all  # type: ignore  # noqa: E402
from jvlink_client.parser import (  # type: ignore  # noqa: E402
    parse_av, parse_cc, parse_jc, parse_tc, parse_we,
)
from builder.live_jvdata import parse_wh  # noqa: E402
from builder import live_status  # noqa: E402


LOCK = ROOT / "out" / "cache" / "fetch-live-jvdata.lock"
CRITICAL_FLAG = ROOT / "out" / "cache" / "live-critical-window.flag"
WINDOW_MINUTES = 90
CRITICAL_WINDOW_MINUTES = 15
STANDARD_ODDS_SECONDS = 5 * 60
RESULT_DELAY_MINUTES = 3
RESULT_TIMEOUT_SECONDS = 2
PRE_RACE_ACTIVITY_MINUTES = 120


def _race_key(race: dict) -> str:
    return (race["race_year"] + race["race_month_day"] + race["track_code"]
            + race["kaiji"] + race["nichiji"] + race["race_num"])


def _result_key(race: dict) -> str:
    """0B12 が要求する短いキー (YYYYMMDD + 競馬場 + レース番号)。"""
    return (race["race_year"] + race["race_month_day"] + race["track_code"]
            + race["race_num"])


def _start(race: dict) -> datetime | None:
    raw = race["race_year"] + race["race_month_day"] + str(race["start_time"] or "")[:4]
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M")
    except ValueError:
        return None


def _result_due(race: dict, now: datetime) -> bool:
    """発走後で、まだ着順を取り込めていないレースだけを0B12の対象にする。"""
    start = _start(race)
    return bool(
        start
        and not race.get("result_confirmed")
        and (now - start).total_seconds() / 60 >= RESULT_DELAY_MINUTES
    )


def _day_update_due(rows: list[dict], now: datetime) -> bool:
    """当日の実発走時刻から、馬体重・馬場を取得する時間帯を決める。"""
    starts = [start for race in rows if (start := _start(race)) is not None]
    if not starts or all(race.get("result_confirmed") for race in rows):
        return False
    return min(starts) - timedelta(minutes=PRE_RACE_ACTIVITY_MINUTES) <= now <= max(starts)


def ingest_wh_files(names: set[str]) -> int:
    count = 0
    raw_dir = KEIBA / "data" / "raw" / "0B11"
    records = []
    for name in names:
        path = raw_dir / Path(name).name
        if not path.is_file():
            continue
        for rec in _split_records(path.read_bytes()):
            got = parse_wh(rec)
            if got:
                records.append(got)
    if not records:
        return 0
    with open_db() as conn:
        for got in records:
            key = got["race_key"]
            race_keys = (key[:4], key[4:8], key[8:10], key[10:12], key[12:14], key[14:16])
            for horse in got["horses"]:
                if horse["horse_weight"] is None:
                    continue
                cur = conn.execute(
                    "UPDATE horse_races SET horse_weight=?,weight_change_sign=?,"
                    "weight_change_diff=? WHERE race_year=? AND race_month_day=? "
                    "AND track_code=? AND kaiji=? AND nichiji=? AND race_num=? "
                    "AND horse_num=?",
                    (str(horse["horse_weight"]), horse["weight_change_sign"],
                     str(horse["weight_change_diff"] or 0), *race_keys,
                     horse["horse_num"]),
                )
                count += cur.rowcount
    return count


def _valid_hhmm(value: object) -> bool:
    raw = str(value or "")
    return (len(raw) == 4 and raw.isdigit()
            and 0 <= int(raw[:2]) <= 23 and 0 <= int(raw[2:]) <= 59)


def _snapshot_rows(conn, table: str, target: str) -> list[dict]:
    try:
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE race_year||race_month_day=?", (target,)
        ).fetchall()]
    except sqlite3.OperationalError:
        return []


def _delete_absent_snapshot_rows(conn, table: str, columns: tuple[str, ...],
                                 current: set[tuple[str, ...]], target: str) -> int:
    rows = _snapshot_rows(conn, table, target)
    removed = 0
    where = " AND ".join(f"{column}=?" for column in columns)
    for row in rows:
        key = tuple(str(row.get(column) or "") for column in columns)
        if key in current:
            continue
        conn.execute(f"DELETE FROM {table} WHERE {where}", key)
        removed += 1
    return removed


def _group_change_rows(rows: list[dict]) -> dict[tuple[str, ...], list[dict]]:
    grouped: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        key = tuple(str(row.get(column) or "") for column in (
            "race_year", "race_month_day", "track_code", "kaiji", "nichiji", "race_num"
        ))
        grouped.setdefault(key, []).append(row)
    return grouped


def _group_change_objects(rows: dict[tuple[str, ...], object]) -> dict[tuple[str, ...], list]:
    grouped: dict[tuple[str, ...], list] = {}
    for key, row in rows.items():
        grouped.setdefault(key[:6], []).append(row)
    return grouped


def _update_race(conn, race_key: tuple[str, ...], assignment: str,
                 values: tuple[object, ...]) -> int:
    cur = conn.execute(
        f"UPDATE races SET {assignment} WHERE race_year=? AND race_month_day=? "
        "AND track_code=? AND kaiji=? AND nichiji=? AND race_num=?",
        (*values, *race_key),
    )
    return cur.rowcount


def reconcile_0b14_snapshot(names: set[str], target: str) -> dict[str, int]:
    """Replace stored 0B14 state and restore withdrawn official changes."""
    raw_dir = KEIBA / "data" / "raw" / "0B14"
    current_av: set[tuple[str, ...]] = set()
    current_jc: set[tuple[str, ...]] = set()
    current_we: set[tuple[str, ...]] = set()
    current_tc: dict[tuple[str, ...], object] = {}
    current_cc: dict[tuple[str, ...], object] = {}
    anomalies = 0
    for name in names:
        path = raw_dir / Path(name).name
        if not path.is_file():
            continue
        for rec in _split_records(path.read_bytes()):
            rt = rec[:2]
            try:
                if rt == b"AV":
                    row = parse_av(rec)
                    current_av.add((row.year, row.month_day, row.track_code, row.kaiji,
                                    row.nichiji, row.race_num, row.horse_num))
                elif rt == b"JC":
                    row = parse_jc(rec)
                    current_jc.add((row.year, row.month_day, row.track_code, row.kaiji,
                                    row.nichiji, row.race_num, row.horse_num))
                elif rt == b"WE":
                    row = parse_we(rec)
                    current_we.add((row.year, row.month_day, row.track_code, row.kaiji,
                                    row.nichiji, row.announced_time))
                elif rt == b"TC":
                    row = parse_tc(rec)
                    if not _valid_hhmm(row.new_start_time):
                        anomalies += 1
                        continue
                    key = (row.year, row.month_day, row.track_code, row.kaiji,
                           row.nichiji, row.race_num, row.announced_time)
                    current_tc[key] = row
                elif rt == b"CC":
                    row = parse_cc(rec)
                    track_type = str(row.new_track_type_code or "")
                    if (row.new_distance <= 0 or len(track_type) != 2
                            or not track_type.isdigit()):
                        anomalies += 1
                        continue
                    key = (row.year, row.month_day, row.track_code, row.kaiji,
                           row.nichiji, row.race_num, row.announced_time)
                    current_cc[key] = row
            except Exception:
                anomalies += 1

    reverted = removed_av = removed_we = restored_tc = restored_cc = 0
    applied_tc = applied_cc = 0
    with open_db() as conn:
        for row in _snapshot_rows(conn, "jockey_changes", target):
            key = tuple(str(row.get(c) or "") for c in (
                "race_year", "race_month_day", "track_code", "kaiji", "nichiji",
                "race_num", "horse_num"))
            if key in current_jc:
                continue
            cur = conn.execute(
                "UPDATE horse_races SET burden_weight=?,jockey_code=?,"
                "jockey_short_name=?,jockey_apprentice_code=? "
                "WHERE race_year=? AND race_month_day=? AND track_code=? AND kaiji=? "
                "AND nichiji=? AND race_num=? AND horse_num=?",
                (row.get("old_burden_weight"), row.get("old_jockey_code"),
                 row.get("old_jockey_name"), row.get("old_apprentice_code"), *key),
            )
            reverted += cur.rowcount
        _delete_absent_snapshot_rows(
            conn, "jockey_changes",
            ("race_year", "race_month_day", "track_code", "kaiji", "nichiji",
             "race_num", "horse_num"), current_jc, target,
        )
        removed_av = _delete_absent_snapshot_rows(
            conn, "race_cancellations",
            ("race_year", "race_month_day", "track_code", "kaiji", "nichiji",
             "race_num", "horse_num"), current_av, target,
        )
        removed_we = _delete_absent_snapshot_rows(
            conn, "weather_going",
            ("race_year", "race_month_day", "track_code", "kaiji", "nichiji",
             "announced_time"), current_we, target,
        )

        tc_rows = _snapshot_rows(conn, "start_time_changes", target)
        current_tc_by_race = _group_change_objects(current_tc)
        for race_key, old_rows in _group_change_rows(tc_rows).items():
            active = current_tc_by_race.get(race_key, [])
            if active:
                latest = max(active, key=lambda row: row.announced_time)
                applied_tc += _update_race(conn, race_key, "start_time=?",
                                           (latest.new_start_time,))
            else:
                original = min(old_rows, key=lambda row: str(row.get("announced_time") or ""))
                if _valid_hhmm(original.get("old_start_time")):
                    restored_tc += _update_race(conn, race_key, "start_time=?",
                                                (original["old_start_time"],))
        _delete_absent_snapshot_rows(
            conn, "start_time_changes",
            ("race_year", "race_month_day", "track_code", "kaiji", "nichiji",
             "race_num", "announced_time"), set(current_tc), target,
        )

        cc_rows = _snapshot_rows(conn, "course_changes", target)
        current_cc_by_race = _group_change_objects(current_cc)
        for race_key, old_rows in _group_change_rows(cc_rows).items():
            active = current_cc_by_race.get(race_key, [])
            if active:
                latest = max(active, key=lambda row: row.announced_time)
                applied_cc += _update_race(
                    conn, race_key, "distance=?,track_type_code=?",
                    (latest.new_distance, latest.new_track_type_code),
                )
            else:
                original = min(old_rows, key=lambda row: str(row.get("announced_time") or ""))
                if int(original.get("old_distance") or 0) > 0 and original.get("old_track_type_code"):
                    restored_cc += _update_race(
                        conn, race_key, "distance=?,track_type_code=?",
                        (original["old_distance"], original["old_track_type_code"]),
                    )
        _delete_absent_snapshot_rows(
            conn, "course_changes",
            ("race_year", "race_month_day", "track_code", "kaiji", "nichiji",
             "race_num", "announced_time"), set(current_cc), target,
        )
    return {
        "jockey_changes_reverted": reverted,
        "cancellations_removed": removed_av,
        "weather_changes_removed": removed_we,
        "start_times_applied": applied_tc,
        "start_times_restored": restored_tc,
        "courses_applied": applied_cc,
        "courses_restored": restored_cc,
        "anomalies": anomalies,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--critical", action="store_true",
                        help="発走15分前の30秒オフセット取得 (0B14/0B30のみ)")
    args = parser.parse_args()
    now = datetime.now()
    target = now.strftime("%Y%m%d")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 20 * 60:
            print("another live fetch is active")
            return 0
        LOCK.unlink(missing_ok=True)
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        with open_db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT r.race_year,r.race_month_day,r.track_code,r.kaiji,r.nichiji,"
                "r.race_num,COALESCE((SELECT tc.new_start_time FROM start_time_changes tc "
                "WHERE tc.race_year=r.race_year AND tc.race_month_day=r.race_month_day "
                "AND tc.track_code=r.track_code AND tc.kaiji=r.kaiji "
                "AND tc.nichiji=r.nichiji AND tc.race_num=r.race_num "
                "ORDER BY tc.announced_time DESC LIMIT 1),r.start_time) start_time,"
                "EXISTS(SELECT 1 FROM horse_races hr "
                "WHERE hr.race_year=r.race_year AND hr.race_month_day=r.race_month_day "
                "AND hr.track_code=r.track_code AND hr.kaiji=r.kaiji "
                "AND hr.nichiji=r.nichiji AND hr.race_num=r.race_num "
                "AND CAST(COALESCE(hr.confirmed_order,'0') AS INTEGER)>0) result_confirmed "
                "FROM races r WHERE r.race_year||r.race_month_day=? ORDER BY r.start_time",
                (target,))]
        if not rows:
            if not args.critical:
                CRITICAL_FLAG.unlink(missing_ok=True)
            print(f"date={target} no_races=1")
            return 0
        if all(race.get("result_confirmed") for race in rows):
            if not args.critical:
                CRITICAL_FLAG.unlink(missing_ok=True)
            print(f"date={target} all_results_confirmed={len(rows)}")
            return 0
        all_upcoming = []
        for race in rows:
            start = _start(race)
            if start and -2 <= (start - now).total_seconds() / 60 <= WINDOW_MINUTES:
                all_upcoming.append(race)
        critical_upcoming = [race for race in all_upcoming if _start(race)
                             and (_start(race) - now).total_seconds() / 60 <=
                             CRITICAL_WINDOW_MINUTES]
        if not args.critical:
            CRITICAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
            if critical_upcoming:
                CRITICAL_FLAG.touch()
            else:
                CRITICAL_FLAG.unlink(missing_ok=True)
        if args.critical:
            upcoming = critical_upcoming
        else:
            upcoming = [
                race for race in all_upcoming
                if race in critical_upcoming or live_status.due(
                    f"0B30:{_race_key(race)}", STANDARD_ODDS_SECONDS)
            ]
        result_due = [] if args.critical else [race for race in rows if _result_due(race, now)]
        day_update_due = bool(critical_upcoming) if args.critical else _day_update_due(rows, now)
        if not day_update_due and not result_due and not upcoming:
            starts = [start for race in rows if (start := _start(race)) is not None]
            schedule = (f"{min(starts):%H:%M}-{max(starts):%H:%M}" if starts else "unknown")
            print(f"date={target} outside_activity_window=1 schedule={schedule}")
            return 0

        results = []
        result_errors = []
        successful_sources: dict[str, str] = {}
        # 0B12 の JVGets が「未配信」を返したとき、定期タスクを長時間占有しない。
        # 取得できなかったレースは次の1分間隔の実行で再試行される。
        os.environ["JVLINK_REALTIME_NO_DATA_SEC"] = str(RESULT_TIMEOUT_SECONDS)
        with JVLinkClient() as client:
            # 開催日単位: 馬体重は発表ごと、馬場は変更ごとに更新される。
            if day_update_due:
                for spec in (("0B14",) if args.critical else ("0B11", "0B14")):
                    results.append((spec, client.fetch_realtime(spec, target)))
                    successful_sources[spec] = target
            # 確定着順・払戻はレース単位で配信される。DBに着順が入ったレースは
            # 次回から除外し、未確定の終了レースだけを追跡する。
            for race in result_due:
                key = _result_key(race)
                try:
                    results.append(("0B12", client.fetch_realtime("0B12", key)))
                    successful_sources[f"0B12:{key}"] = key
                except Exception as exc:
                    result_errors.append(f"{key}:{type(exc).__name__}")
            # 全賭式は選択肢が膨大なので、直近90分のレースだけを取得する。
            for race in upcoming:
                key = _race_key(race)
                results.append(("0B30", client.fetch_realtime("0B30", key)))
                successful_sources[f"0B30:{key}"] = key

        wh_names = {name for spec, result in results if spec == "0B11"
                    for name in (result.get("filenames") or [])}
        weight_rows = ingest_wh_files(wh_names)
        snapshot_stats = {
            "jockey_changes_reverted": 0, "cancellations_removed": 0,
            "weather_changes_removed": 0, "start_times_applied": 0,
            "start_times_restored": 0, "courses_applied": 0,
            "courses_restored": 0, "anomalies": 0,
        }
        for spec in ("0B12", "0B14", "0B30"):
            names = {name for got_spec, result in results if got_spec == spec
                     for name in (result.get("filenames") or [])}
            if names:
                ingest_all(dataspecs=[spec], only_files=names)
            # An empty successful 0B14 response means that no changes remain;
            # it must therefore withdraw previously stored snapshot rows.
            if spec == "0B14" and any(got_spec == spec for got_spec, _ in results):
                snapshot_stats = reconcile_0b14_snapshot(names, target)
                if snapshot_stats["anomalies"]:
                    raise RuntimeError(
                        f"invalid 0B14 records: {snapshot_stats['anomalies']}"
                    )
        live_status.record_success(successful_sources)
        print(f"date={target} day_update_due={int(day_update_due)} upcoming={len(upcoming)} "
              f"critical={int(args.critical)} "
              f"result_due={len(result_due)} "
              f"result_errors={','.join(result_errors) or '-'} weight_rows={weight_rows} "
              f"jc_reverted={snapshot_stats['jockey_changes_reverted']} "
              f"av_removed={snapshot_stats['cancellations_removed']} "
              f"we_removed={snapshot_stats['weather_changes_removed']} "
              f"tc_applied={snapshot_stats['start_times_applied']} "
              f"tc_restored={snapshot_stats['start_times_restored']} "
              f"cc_applied={snapshot_stats['courses_applied']} "
              f"cc_restored={snapshot_stats['courses_restored']} "
              f"files={sum(int(r.get('files_written') or 0) for _, r in results)}")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
