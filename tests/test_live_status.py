from __future__ import annotations

from datetime import datetime, timezone

from builder import live_status


def test_live_status_records_success_and_checks_age_and_target(tmp_path, monkeypatch):
    monkeypatch.setattr(live_status, "STATUS_PATH", tmp_path / "status.json")
    at = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
    live_status.record_success({"0B14": "20260823"}, now=at)
    fresh = live_status.source("0B14", target="20260823",
                               now=at.timestamp() + 60, max_age_seconds=90)
    assert fresh["fresh"] is True and fresh["age_seconds"] == 60.0
    assert live_status.source("0B14", target="20260824",
                              now=at.timestamp() + 60)["fresh"] is False
    assert live_status.due("0B14", 90, now=at.timestamp() + 91) is True


def test_failed_or_missing_source_is_never_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(live_status, "STATUS_PATH", tmp_path / "missing.json")
    got = live_status.source("0B14", target="20260823")
    assert got["fresh"] is False and got["checked_at"] is None


# ---------------------------------------------------------------------------
# 開催が無い時間帯と、本当に止まった状態を区別する
# ---------------------------------------------------------------------------
# `max_age_seconds` (90秒) は開催中の速報間隔を前提にした閾値なので、
# 開催が無い日は必ず超える。それを `fresh=False` として返していたため、
# **平日は常に「速報情報を更新中」が出たまま**になり、本当に取り込みが
# 止まったときに区別がつかなかった。
def _status(tmp_path, monkeypatch, checked_at: str, target: str = "20260919"):
    path = tmp_path / "live-jvdata-status.json"
    monkeypatch.setattr(live_status, "STATUS_PATH", path)
    path.write_text(
        '{"sources": {"0B14": {"checked_at": "%s", "target": "%s"}}}'
        % (checked_at, target), encoding="utf-8")
    return path


def test_an_old_record_is_idle_when_no_fetch_is_expected(tmp_path, monkeypatch):
    """取り込みが動かない時間帯は鮮度を判定しないこと。

    ここで `fresh=False` を返すと、画面は平日じゅう「更新中」を出し続ける。
    直しようがない警告は、直せる警告を埋もれさせる。
    """
    _status(tmp_path, monkeypatch, "2026-09-13T16:31:34+09:00")
    got = live_status.source("0B14", target="20260919", expected=False)
    assert got["state"] == "idle"
    assert got["fresh"] is None            # UI の `=== false` に当たらない
    assert got["age_seconds"] > 90         # 古いこと自体は隠さない


def test_an_old_record_is_stale_when_a_fetch_is_expected(tmp_path, monkeypatch):
    """動くはずの時間帯に古ければ **これだけが異常**。"""
    _status(tmp_path, monkeypatch, "2026-09-13T16:31:34+09:00")
    got = live_status.source("0B14", target="20260919", expected=True)
    assert got["state"] == "stale"
    assert got["fresh"] is False


def test_a_recent_record_is_fresh(tmp_path, monkeypatch):
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    _status(tmp_path, monkeypatch, now.isoformat(timespec="seconds"))
    got = live_status.source("0B14", target="20260919", expected=True,
                             now=now.timestamp() + 10)
    assert got["state"] == "fresh" and got["fresh"] is True


def test_a_missing_record_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(live_status, "STATUS_PATH", tmp_path / "none.json")
    got = live_status.source("0B14", target="20260919", expected=True)
    assert got["state"] == "unknown"
    assert got["fresh"] is False           # 一度も取れていないのは異常


def test_the_default_behaviour_is_unchanged(tmp_path, monkeypatch):
    """`expected` を渡さなければ従来どおり bool を返すこと。

    取得タスク側の `due()` は「呼ばれている時点で取り込みの時間帯にいる」ので
    挙動を変えない。
    """
    _status(tmp_path, monkeypatch, "2026-09-13T16:31:34+09:00")
    got = live_status.source("0B14", target="20260919")
    assert got["fresh"] is False and got["expected"] is None
    assert live_status.due("0B14", 90) is True
