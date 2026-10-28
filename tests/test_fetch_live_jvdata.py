from __future__ import annotations

from datetime import datetime
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

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


# ---- 止まった実行を残さない (2026-10-25 に JV-Link の応答待ちで 10 組が残った) ----

def _isolate(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "LOCK", tmp_path / "cache" / "fetch-live-jvdata.lock")
    monkeypatch.setattr(mod, "CRITICAL_FLAG", tmp_path / "cache" / "live-critical-window.flag")
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "logs")


def _exited_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_lock_is_kept_while_its_owner_lives_and_released_only_by_the_owner(tmp_path, monkeypatch):
    mod = _module()
    _isolate(mod, tmp_path, monkeypatch)
    now = time.time()
    token = mod._acquire_lock(now)
    assert token
    assert mod._acquire_lock(now + 60) is None          # 持ち主 (このプロセス) は生きている
    mod._release_lock("an-older-run")                    # 後で動き出した古い実行
    assert mod.LOCK.exists()
    mod._release_lock(token)
    assert not mod.LOCK.exists()


@pytest.mark.parametrize("owner", ["exited", "pid_reused"])
def test_lock_of_a_dead_owner_is_taken_over_at_once(tmp_path, monkeypatch, owner):
    mod = _module()
    _isolate(mod, tmp_path, monkeypatch)
    mod.LOCK.parent.mkdir(parents=True)
    pid = _exited_pid() if owner == "exited" else os.getpid()
    mod.LOCK.write_text(json.dumps({"pid": pid, "created": 1, "token": "old"}),
                        encoding="utf-8")
    assert mod._acquire_lock(time.time()) is not None
    assert json.loads(mod.LOCK.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_lock_without_an_owner_is_taken_over_only_when_old(tmp_path, monkeypatch):
    mod = _module()
    _isolate(mod, tmp_path, monkeypatch)
    mod.LOCK.parent.mkdir(parents=True)
    mod.LOCK.write_text("", encoding="utf-8")            # 旧版のロック、または書きかけ
    assert mod._acquire_lock(time.time()) is None
    old = time.time() - mod.LOCK_STALE_SECONDS - 1
    os.utime(mod.LOCK, (old, old))
    assert mod._acquire_lock(time.time()) is not None


def test_watchdog_ends_a_process_stuck_in_a_call_that_holds_the_gil(tmp_path):
    """JV-Link の COM 呼び出しの代わりに、GIL を持ったまま戻らない kernel32.Sleep で止める。"""
    script = Path("deploy/windows/fetch-live-jvdata.py").resolve()
    code = textwrap.dedent(f"""
        import ctypes, importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("m", r"{script}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.LOG_DIR = pathlib.Path(r"{tmp_path}")
        mod._LOG = mod._open_log(mod.datetime.now())
        mod._step("JVRTOpen stand-in", 1)
        class PyWinDLL(ctypes.CDLL):
            _func_flags_ = ctypes._FUNCFLAG_STDCALL | ctypes._FUNCFLAG_PYTHONAPI
        PyWinDLL("kernel32").Sleep(60000)
    """)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], timeout=45, capture_output=True)
    assert proc.returncode == 1, proc.stderr
    assert time.monotonic() - started < 30
    log = next(tmp_path.glob("live-jvdata-*.log")).read_text(encoding="utf-8")
    assert "step JVRTOpen stand-in" in log
    assert "Timeout (0:00:01)!" in log and 'File "<string>"' in log


class _Clock(datetime):
    current = datetime(2026, 10, 25, 15, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _race_day_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE races (race_year TEXT,race_month_day TEXT,track_code TEXT,kaiji TEXT,
        nichiji TEXT,race_num TEXT,start_time TEXT);
      CREATE TABLE start_time_changes (race_year TEXT,race_month_day TEXT,track_code TEXT,
        kaiji TEXT,nichiji TEXT,race_num TEXT,new_start_time TEXT,announced_time TEXT);
      CREATE TABLE horse_races (race_year TEXT,race_month_day TEXT,track_code TEXT,
        kaiji TEXT,nichiji TEXT,race_num TEXT,confirmed_order TEXT);
      INSERT INTO races VALUES ('2026','1025','05','04','08','11','1540');
      INSERT INTO races VALUES ('2026','1025','05','04','08','10','1450');
    """)
    return conn


_NO_SNAPSHOT_CHANGES = dict.fromkeys((
    "jockey_changes_reverted", "cancellations_removed", "weather_changes_removed",
    "start_times_applied", "start_times_restored", "courses_applied",
    "courses_restored", "anomalies"), 0)


@pytest.mark.parametrize("resume", [None, "late", "next_day"])
def test_a_run_that_resumes_late_writes_nothing(tmp_path, monkeypatch, resume):
    """本命の経路 (main) で、JV-Link の応答待ちから遅れて戻った実行が書き込まないこと。
    resume=None は対照 (同じ経路で書き込みが起きることを確かめる)。"""
    mod = _module()
    _isolate(mod, tmp_path, monkeypatch)
    _Clock.current = datetime(2026, 10, 25, 15, 0)
    monkeypatch.setattr(mod, "datetime", _Clock)
    conn = _race_day_db()
    monkeypatch.setattr(mod, "open_db", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(mod.live_status, "due", lambda *a, **k: True)
    events = []
    monkeypatch.setattr(mod.faulthandler, "dump_traceback_later",
                        lambda seconds, **kw: events.append(("arm", seconds)))
    monkeypatch.setattr(mod.faulthandler, "cancel_dump_traceback_later",
                        lambda: events.append(("cancel",)))
    monkeypatch.setattr(mod, "ingest_wh_files",
                        lambda names: events.append(("write", "0B11")) or 0)
    monkeypatch.setattr(mod, "ingest_all",
                        lambda dataspecs, only_files: events.append(("write", dataspecs[0])))
    monkeypatch.setattr(mod, "reconcile_0b14_snapshot",
                        lambda names, target: events.append(("write", "reconcile"))
                        or dict(_NO_SNAPSHOT_CHANGES))
    monkeypatch.setattr(mod.live_status, "record_success",
                        lambda sources: events.append(("write", "status")))
    real_time = time.time

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch_realtime(self, spec, key):
            events.append(("fetch", spec))
            if resume == "late":         # 応答待ちで止まり、30 分後に戻ってきた
                monkeypatch.setattr(mod.time, "time", lambda: real_time() + 30 * 60)
            elif resume == "next_day":
                _Clock.current = datetime(2026, 10, 26, 8, 0)
            return {"filenames": [f"{spec}_{key}.jvd"], "files_written": 1}

    monkeypatch.setattr(mod, "JVLinkClient", Client)
    monkeypatch.setattr(sys, "argv", ["fetch-live-jvdata.py"])

    got = mod.main()

    writes = [e[1] for e in events if e[0] == "write"]
    if resume is None:
        assert got == 0
        assert writes == ["0B11", "0B12", "0B14", "reconcile", "0B30", "status"]
    else:
        assert got == 3 and writes == []
    # 取得の前には必ず上限をかけ直している。終わりに解除し、ロックは残さない。
    fetches = [i for i, e in enumerate(events) if e[0] == "fetch"]
    assert fetches and all(events[i - 1] == ("arm", mod.STEP_TIMEOUT_SECONDS) for i in fetches)
    assert events[-1] == ("cancel",)
    assert not mod.LOCK.exists()
    log = next((tmp_path / "logs").glob("live-jvdata-*.log")).read_text(encoding="utf-8")
    assert "step 0B30:202610250504081" in log
    assert ("stale run" in log) == (resume is not None)
