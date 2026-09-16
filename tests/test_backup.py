from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from builder.backup import create_backup


def _installation(tmp_path):
    cache = tmp_path / "out" / "cache"
    cache.mkdir(parents=True)
    auth_db = cache / "auth.db"
    with sqlite3.connect(auth_db) as conn:
        conn.execute("CREATE TABLE users (name TEXT)")
        conn.execute("INSERT INTO users VALUES ('test user')")
    (cache / "configs.json").write_text('{"owner": {}}', encoding="utf-8")
    config = tmp_path / "shared.config.json"
    config.write_text(json.dumps({
        "workdir": str(tmp_path),
        "auth_db": str(auth_db),
        "admin_password": "secret-value",
    }), encoding="utf-8")
    return config


def test_create_backup_copies_a_consistent_database_and_settings(tmp_path):
    config = _installation(tmp_path)
    snapshot = create_backup(
        config,
        now=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
    )

    with sqlite3.connect(snapshot / "auth.db") as conn:
        assert conn.execute("SELECT name FROM users").fetchone() == ("test user",)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert json.loads((snapshot / "cache" / "configs.json").read_text(encoding="utf-8"))
    assert json.loads((snapshot / "service" / "shared.config.json").read_text(
        encoding="utf-8"))["admin_password"] == "secret-value"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in manifest["files"]} == {
        "auth.db", "cache/configs.json", "service/shared.config.json",
    }


def test_create_backup_prunes_old_snapshots(tmp_path):
    config = _installation(tmp_path)
    for hour in range(4):
        create_backup(
            config,
            keep=2,
            now=datetime(2026, 8, 1, hour, 0, tzinfo=timezone.utc),
        )

    snapshots = sorted(path.name for path in (tmp_path / "out" / "backups").glob("backup-*"))
    assert snapshots == ["backup-20260801-020000", "backup-20260801-030000"]

