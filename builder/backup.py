"""Create verified, rotating backups for a shared MAIBuilder installation."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "deploy" / "windows" / "shared.config.json"
OPTIONAL_CACHE_FILES = (
    "configs.json",
    "applied_configs.json",
    "purchases.json",
    "preset_weights.json",
    "preset_weights_percol.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_json(source: Path, destination: Path) -> None:
    # Refuse to preserve a malformed settings file as a valid backup.
    json.loads(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(destination)) as backup_db:
        source_db.backup(backup_db)
        result = backup_db.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result!r}")


def _unique_destination(backup_root: Path, stamp: str) -> Path:
    candidate = backup_root / f"backup-{stamp}"
    suffix = 1
    while candidate.exists() or candidate.with_name(f".{candidate.name}.tmp").exists():
        candidate = backup_root / f"backup-{stamp}-{suffix:02d}"
        suffix += 1
    return candidate


def _prune(backup_root: Path, keep: int) -> None:
    snapshots = sorted(
        (path for path in backup_root.glob("backup-*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for expired in snapshots[keep:]:
        shutil.rmtree(expired)


def create_backup(
    config_path: Path = DEFAULT_CONFIG,
    *,
    destination: Path | None = None,
    keep: int = 14,
    now: datetime | None = None,
) -> Path:
    if keep < 1:
        raise ValueError("keep must be at least 1")

    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    workdir = Path(config.get("workdir", ROOT)).resolve()
    auth_db = Path(config.get("auth_db", workdir / "out" / "cache" / "auth.db")).resolve()
    backup_root = (destination or (workdir / "out" / "backups")).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)

    if not auth_db.is_file():
        raise FileNotFoundError(f"Authentication database was not found: {auth_db}")

    created_at = (now or datetime.now().astimezone())
    final_dir = _unique_destination(backup_root, created_at.strftime("%Y%m%d-%H%M%S"))
    temp_dir = final_dir.with_name(f".{final_dir.name}.tmp")
    temp_dir.mkdir()

    try:
        files: list[dict[str, object]] = []

        db_destination = temp_dir / "auth.db"
        _backup_sqlite(auth_db, db_destination)
        files.append({"name": "auth.db", "size": db_destination.stat().st_size,
                      "sha256": _sha256(db_destination)})

        cache_dir = workdir / "out" / "cache"
        for name in OPTIONAL_CACHE_FILES:
            source = cache_dir / name
            if not source.is_file():
                continue
            copied = temp_dir / "cache" / name
            _copy_json(source, copied)
            files.append({"name": f"cache/{name}", "size": copied.stat().st_size,
                          "sha256": _sha256(copied)})

        config_destination = temp_dir / "service" / "shared.config.json"
        _copy_json(config_path, config_destination)
        files.append({"name": "service/shared.config.json",
                      "size": config_destination.stat().st_size,
                      "sha256": _sha256(config_destination)})

        manifest = {
            "format": 1,
            "created_at": created_at.isoformat(),
            "files": files,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    _prune(backup_root, keep)
    return final_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    snapshot = create_backup(args.config, destination=args.destination, keep=args.keep)
    print(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
