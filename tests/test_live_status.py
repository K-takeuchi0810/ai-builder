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
