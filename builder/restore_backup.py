"""Verify that a MAIBuilder backup can be restored into an isolated directory."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(name: object) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("Backup manifest contains an invalid file name")
    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise ValueError(f"Unsafe path in backup manifest: {name!r}")
    return Path(*posix.parts)


def restore_to_isolated_directory(snapshot: Path, destination: Path) -> dict[str, object]:
    snapshot = snapshot.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Restore destination already exists: {destination}")

    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported or malformed backup manifest")

    destination.mkdir(parents=True)
    try:
        restored_files: list[Path] = []
        for entry in manifest["files"]:
            relative = _safe_relative_path(entry.get("name"))
            source = snapshot / relative
            copied = destination / relative
            if not source.is_file():
                raise FileNotFoundError(f"Backup file is missing: {relative.as_posix()}")
            if source.stat().st_size != entry.get("size") or _sha256(source) != entry.get("sha256"):
                raise ValueError(f"Backup file failed verification: {relative.as_posix()}")
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copied)
            if _sha256(copied) != entry.get("sha256"):
                raise ValueError(f"Restored file failed verification: {relative.as_posix()}")
            if copied.suffix.lower() == ".json":
                json.loads(copied.read_text(encoding="utf-8"))
            restored_files.append(copied)

        database = destination / "auth.db"
        if not database.is_file():
            raise FileNotFoundError("Restored authentication database is missing")
        with closing(sqlite3.connect(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Restored SQLite integrity check failed: {integrity!r}")
        required_tables = {"users", "invites", "sessions"}
        if not required_tables.issubset(tables):
            raise RuntimeError(f"Restored database is missing tables: {required_tables - tables}")

        return {
            "file_count": len(restored_files),
            "sqlite_integrity": integrity[0],
            "required_tables": sorted(required_tables),
        }
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def test_restore(snapshot: Path) -> dict[str, object]:
    snapshot = snapshot.resolve()
    temporary = Path(tempfile.mkdtemp(prefix=".restore-test-", dir=snapshot.parent))
    temporary.rmdir()
    try:
        return restore_to_isolated_directory(snapshot, temporary)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    result = test_restore(args.snapshot)
    print("restore_test=ok")
    print(f"file_count={result['file_count']}")
    print(f"sqlite_integrity={result['sqlite_integrity']}")
    print("temporary_copy_removed=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

