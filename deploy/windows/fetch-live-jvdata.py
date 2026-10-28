"""MAIBuilder用の馬体重・馬場・全券種オッズ・確定結果をJV-Linkから更新する。

32-bit Pythonで実行する。JV-Link公式仕様の速報データだけを保存し、取得できない
値を推測しない。MAIBuilder本体は更新されたkeiba.dbをread-onlyで参照する。
"""

from __future__ import annotations

import os
import argparse
import faulthandler
import json
import logging
import sqlite3
import sys
import time
import traceback
import uuid
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

# JV-Link の呼び出しが応答を返さないまま止まることがある (2026-10-25 は 10 回)。タスクの
# 実行時間の上限 (4 分) は wscript だけを止めるので、cmd と python が残り、ロックを 20 分
# つかんだままになっていた。区切りごとに上限をかけ直し、超えたら止まった場所を記録して自分で終わる。
LOG_DIR = ROOT / "out" / "logs" / "live-jvdata"
LOG_KEEP_DAYS = 14
# JV-Link の 1 回の取得。JVRTOpen の再試行 (待ち 5・15・45 秒) のたびにかけ直す。成功した実行は
# 1 分の中で長くても 55 秒で終わっている (live-jvdata-status.json の 24 開催日、2026-10-28 に確認)
STEP_TIMEOUT_SECONDS = 120
INGEST_TIMEOUT_SECONDS = 300    # 取り込みの 1 段 (DB のロック待ちを含む)
# 起動からこれより後には取得も書き込みも続けない。止まっていた実行が後で動き出し、古い状態で
# 照合するのを防ぐ。見張りと合わせ、生きている実行は長くてもこれ + 取り込み 1 段で終わる。
RUN_MAX_SECONDS = 10 * 60
# 持ち主を確かめられないロック (書きかけ・旧版) を古いとみなす時間。持ち主が生きているロックは奪わない。
LOCK_STALE_SECONDS = RUN_MAX_SECONDS + 5 * 60

_LOG = None
# テストで差し替える (プロセス全体の time.time を替えない)。起動からの経過だけに使い、ロックの古さは
# ファイルの mtime (実時計) と比べる
_wall_clock = time.time
LIVE_SPECS = ("0B11", "0B12", "0B14", "0B30")


class StaleRun(RuntimeError):
    """起動から時間がたちすぎた、または日付が変わった実行。"""


def _log(message: str) -> None:
    if _LOG is not None:
        _LOG.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S} pid={os.getpid()} {message}\n")
        _LOG.flush()


def _open_log(now: datetime):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for old in LOG_DIR.glob("live-jvdata-*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return open(LOG_DIR / f"live-jvdata-{now:%Y%m%d}.log", "a", encoding="utf-8")


def _report(message: str) -> None:
    print(message)
    _log(message)


def _step(label: str, seconds: int = STEP_TIMEOUT_SECONDS) -> None:
    """次の区切りまでの上限をかけ直す。超えると faulthandler が全スレッドの
    traceback をログに書いてプロセスを終える (GIL を持ったままの COM 呼び出しでも効く)。"""
    _log(f"step {label}")
    faulthandler.dump_traceback_later(seconds, exit=True, file=_LOG or sys.stderr)


def _on_jvlink_progress(event: str, info: dict) -> None:
    """再試行の待ちは正常な遅れなので、上限をかけ直して見張りに殺させない。未配信の待ち (rt_wait) は
    JVLINK_REALTIME_NO_DATA_SEC で切れるのでかけ直さない。"""
    if event == "rt_retry":
        _step(f"retry {info.get('dataspec')}:{info.get('key')} rc={info.get('rc')} "
              f"attempt={info.get('attempt')} wait={info.get('wait_sec')}s")


def _log_leftover_parts() -> None:
    """見張りで終わった実行が残した書きかけ (.jvd.part) の数をログに残す。取り込まれないので消さない
    (止まった場所の調べに使う)。"""
    counts = []
    for spec in LIVE_SPECS:
        try:
            with os.scandir(KEIBA / "data" / "raw" / spec) as entries:
                n = sum(1 for e in entries if e.name.endswith(".jvd.part"))
        except OSError:
            continue
        if n:
            counts.append(f"{spec}={n}")
    if counts:
        _log("leftover .jvd.part " + " ".join(counts))


def _ensure_current(started: float, target: str, label: str) -> None:
    elapsed = _wall_clock() - started
    today = datetime.now().strftime("%Y%m%d")
    if elapsed > RUN_MAX_SECONDS or today != target:
        raise StaleRun(f"{label}: elapsed={int(elapsed)}s target={target} today={today}")


def _process_created(pid: int) -> int | None:
    """生きているプロセスの作成時刻 (FILETIME)。番号の使い回しを見分けるのに使う。
    終わっていれば None、確かめられなければ -1 (生きているとみなす)。"""
    if sys.platform != "win32":
        return -1
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # 87 = ERROR_INVALID_PARAMETER (その番号のプロセスが無い)。それ以外は確かめられない
        return None if ctypes.get_last_error() == 87 else -1
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return -1
        if code.value != 259:  # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return -1
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k32.CloseHandle(handle)


def _lock_stale_reason(now: float) -> str | None:
    """ロックを引き継いでよい理由。持ち主が生きていれば None (古くても奪わない。生きている実行は
    見張りと RUN_MAX_SECONDS で終わるので、奪うと JV-Link を 2 つの実行が同時に使うだけになる)。"""
    try:
        age = now - LOCK.stat().st_mtime
    except FileNotFoundError:
        return "gone"
    try:
        owner = json.loads(LOCK.read_text(encoding="utf-8"))
        pid, created = int(owner["pid"]), owner.get("created")
    except (OSError, ValueError, TypeError, KeyError):
        # 書きかけ・旧版のロック。古くなるまで待つ
        return f"no owner, age={int(age)}s" if age >= LOCK_STALE_SECONDS else None
    alive = _process_created(pid)
    if alive is None:
        return f"owner pid={pid} exited"
    if alive != -1 and created not in (None, -1) and alive != created:
        return f"owner pid={pid} reused"
    if age >= 2 * LOCK_STALE_SECONDS:
        # 見張りも RUN_MAX_SECONDS も効かずに居座っている (想定外)。最後の安全弁
        return f"owner pid={pid} alive but age={int(age)}s"
    return None


def _acquire_lock(now: float) -> str | None:
    """取れたら持ち主の印を返す。ほかの実行が生きていれば None。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    body = json.dumps({"pid": os.getpid(), "created": _process_created(os.getpid()),
                       "token": token, "started_at": now})
    for _ in range(2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            reason = _lock_stale_reason(now)
            if reason is None:
                return None
            _log(f"lock taken over: {reason}")
            LOCK.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        return token
    return None


def _release_lock(token: str) -> None:
    """自分のロックだけを消す。後で動き出した古い実行が、今の実行のロックを消さない。"""
    try:
        owner = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(owner, dict) and owner.get("token") == token:
        LOCK.unlink(missing_ok=True)


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


def _run(args, now: datetime, started: float) -> int:
    target = now.strftime("%Y%m%d")
    _step("read races")
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
        _report(f"date={target} no_races=1")
        return 0
    if all(race.get("result_confirmed") for race in rows):
        if not args.critical:
            CRITICAL_FLAG.unlink(missing_ok=True)
        _report(f"date={target} all_results_confirmed={len(rows)}")
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
        _report(f"date={target} outside_activity_window=1 schedule={schedule}")
        return 0

    results = []
    result_errors = []
    successful_sources: dict[str, str] = {}
    # 0B12 の JVGets が「未配信」を返したとき、定期タスクを長時間占有しない。
    # 取得できなかったレースは次の1分間隔の実行で再試行される。
    os.environ["JVLINK_REALTIME_NO_DATA_SEC"] = str(RESULT_TIMEOUT_SECONDS)
    def fetch_step(label: str) -> None:
        # 取得の途中でも RUN_MAX_SECONDS を超えたら止める (遅い取得が続いても次の実行と重ならない)
        _ensure_current(started, target, label)
        _step(label)

    fetch_step("JVInit")
    with JVLinkClient() as client:
        # 開催日単位: 馬体重は発表ごと、馬場は変更ごとに更新される。
        if day_update_due:
            for spec in (("0B14",) if args.critical else ("0B11", "0B14")):
                fetch_step(f"{spec}:{target}")
                results.append((spec, client.fetch_realtime(spec, target, on_progress=_on_jvlink_progress)))
                successful_sources[spec] = target
        # 確定着順・払戻はレース単位で配信される。DBに着順が入ったレースは
        # 次回から除外し、未確定の終了レースだけを追跡する。
        for race in result_due:
            key = _result_key(race)
            fetch_step(f"0B12:{key}")
            try:
                results.append(("0B12", client.fetch_realtime("0B12", key,
                                                            on_progress=_on_jvlink_progress)))
                successful_sources[f"0B12:{key}"] = key
            except Exception as exc:
                result_errors.append(f"{key}:{type(exc).__name__}")
        # 全賭式は選択肢が膨大なので、直近90分のレースだけを取得する。
        for race in upcoming:
            key = _race_key(race)
            fetch_step(f"0B30:{key}")
            results.append(("0B30", client.fetch_realtime("0B30", key,
                                                        on_progress=_on_jvlink_progress)))
            successful_sources[f"0B30:{key}"] = key
        _step("JVClose")

    wh_names = {name for spec, result in results if spec == "0B11"
                for name in (result.get("filenames") or [])}
    _ensure_current(started, target, "0B11")
    _step("ingest 0B11", INGEST_TIMEOUT_SECONDS)
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
        _ensure_current(started, target, spec)
        _step(f"ingest {spec}", INGEST_TIMEOUT_SECONDS)
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
    _ensure_current(started, target, "status")
    live_status.record_success(successful_sources)
    _report(f"date={target} day_update_due={int(day_update_due)} upcoming={len(upcoming)} "
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


def main() -> int:
    global _LOG
    parser = argparse.ArgumentParser()
    parser.add_argument("--critical", action="store_true",
                        help="発走15分前の30秒オフセット取得 (0B14/0B30のみ)")
    args = parser.parse_args()
    now = datetime.now()
    started = _wall_clock()
    _LOG = _open_log(now)
    # jvlink_client などの logger.warning (raw の名前を変えられない等) も日別ログへ。stderr は捨てられる
    handler = logging.StreamHandler(_LOG)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s pid={os.getpid()} log %(levelname)s %(name)s: %(message)s",
        "%Y-%m-%dT%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    try:
        _log(f"start critical={int(args.critical)}")
        token = _acquire_lock(started)
        if token is None:
            _report("another live fetch is active")
            return 0
        _log_leftover_parts()
        try:
            return _run(args, now, started)
        except StaleRun as exc:
            # その段より後は書かずに終える。各段の取り込みは冪等なので、次の 1 分の実行がやり直す。
            _report(f"stale run, stopped before writing {exc}")
            return 3
        except BaseException as exc:
            # 起動経路 (vbs の非表示実行) は stderr を捨てるので、traceback はここにしか残らない
            _log(f"error {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()
            _release_lock(token)
            _log(f"end {_wall_clock() - started:.1f}s")
    finally:
        logging.getLogger().removeHandler(handler)
        _LOG.close()
        _LOG = None


if __name__ == "__main__":
    raise SystemExit(main())
