"""JV-Link速報取得の成功時刻を、APIと32bit取得タスクで共有する。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time

from . import config


STATUS_PATH = Path(config.CORNER_INDEX_PATH).parent / "live-jvdata-status.json"
QR_MAX_AGE_SECONDS = 90


def load() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def record_success(sources: dict[str, str], *, now: datetime | None = None) -> dict:
    """成功した取得元だけを更新する。失敗時刻で正常時刻を上書きしない。"""
    if not sources:
        return load()
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    data = load()
    current = data.setdefault("sources", {})
    for source, target in sources.items():
        current[str(source)] = {"checked_at": stamp, "target": str(target or "")}
    data["updated_at"] = stamp
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(STATUS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_PATH)
    return data


def source(source: str, *, target: str = "", now: float | None = None,
           max_age_seconds: int = QR_MAX_AGE_SECONDS) -> dict:
    row = ((load().get("sources") or {}).get(source) or {})
    checked_at = str(row.get("checked_at") or "")
    age = None
    try:
        age = max(0.0, (now if now is not None else time.time())
                  - datetime.fromisoformat(checked_at).timestamp())
    except (TypeError, ValueError):
        pass
    target_ok = not target or str(row.get("target") or "") == str(target)
    fresh = age is not None and age <= max_age_seconds and target_ok
    return {
        "source": source, "checked_at": checked_at or None,
        "target": row.get("target") or None,
        "age_seconds": round(age, 1) if age is not None else None,
        "max_age_seconds": max_age_seconds, "fresh": fresh,
    }


def due(source_key: str, seconds: int, *, now: float | None = None) -> bool:
    return not source(source_key, now=now, max_age_seconds=seconds)["fresh"]
