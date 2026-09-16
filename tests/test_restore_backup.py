from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from builder.backup import create_backup
from builder.restore_backup import restore_to_isolated_directory, test_restore as run_restore_test


def _snapshot(tmp_path):
    cache = tmp_path / "out" / "cache"
    cache.mkdir(parents=True)
    auth_db = cache / "auth.db"
    with sqlite3.connect(auth_db) as connection:
        connection.executescript("""
            CREATE TABLE users (id TEXT);
            CREATE TABLE invites (id TEXT);
            CREATE TABLE sessions (id TEXT);
            INSERT INTO users VALUES ('user-1');
        """)
    (cache / "configs.json").write_text('{"user-1": {}}', encoding="utf-8")
    config = tmp_path / "shared.config.json"
    config.write_text(json.dumps({
        "workdir": str(tmp_path),
        "auth_db": str(auth_db),
        "admin_password": "secret-value",
    }), encoding="utf-8")
    return create_backup(
        config,
        now=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
    )


def test_restore_copies_and_validates_backup(tmp_path):
    snapshot = _snapshot(tmp_path)
    destination = tmp_path / "restored"

    result = restore_to_isolated_directory(snapshot, destination)

    assert result["sqlite_integrity"] == "ok"
    assert result["file_count"] == 3
    with sqlite3.connect(destination / "auth.db") as connection:
        assert connection.execute("SELECT id FROM users").fetchone() == ("user-1",)


def test_restore_test_removes_its_temporary_copy(tmp_path):
    snapshot = _snapshot(tmp_path)

    assert run_restore_test(snapshot)["sqlite_integrity"] == "ok"
    assert not list(snapshot.parent.glob(".restore-test-*"))


def test_restore_rejects_a_tampered_backup(tmp_path):
    snapshot = _snapshot(tmp_path)
    (snapshot / "auth.db").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="failed verification"):
        restore_to_isolated_directory(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_restore_rejects_manifest_path_traversal(tmp_path):
    snapshot = _snapshot(tmp_path)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["name"] = "../outside.db"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe path"):
        restore_to_isolated_directory(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
