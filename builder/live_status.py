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
           max_age_seconds: int = QR_MAX_AGE_SECONDS,
           expected: bool | None = None) -> dict:
    """取得元の鮮度。**取り込みが動くはずの時間帯かどうかを分けて返す。**

    `max_age_seconds` は開催中の速報間隔 (90秒) を前提にした閾値なので、
    開催が無い日や時間帯には必ず「鮮度切れ」になる。それをそのまま `fresh=False`
    として返していたため、**平日は常に警告が出たままになり、本当に取り込みが
    止まったときに区別がつかなかった**。

    `expected=False` (取り込みが動かない時間帯) のときは `fresh` を None にし、
    `state` を "idle" にする。`expected` を渡さなければ従来どおりの bool を返す
    (取得タスク側の `due()` は挙動を変えない)。

    state:
      fresh   … 期待どおり新しい
      stale   … 動くはずの時間帯なのに古い ← **これだけが異常**
      idle    … 取り込みが動かない時間帯 (開催なし等)。鮮度は判定しない
      unknown … 一度も記録が無い
    """
    row = ((load().get("sources") or {}).get(source) or {})
    checked_at = str(row.get("checked_at") or "")
    age = None
    try:
        age = max(0.0, (now if now is not None else time.time())
                  - datetime.fromisoformat(checked_at).timestamp())
    except (TypeError, ValueError):
        pass
    target_ok = not target or str(row.get("target") or "") == str(target)
    is_fresh = age is not None and age <= max_age_seconds and target_ok

    if expected is False:
        state, fresh = "idle", None
    elif age is None:
        state, fresh = "unknown", is_fresh
    elif is_fresh:
        state, fresh = "fresh", True
    else:
        state, fresh = "stale", False
    return {
        "source": source, "checked_at": checked_at or None,
        "target": row.get("target") or None,
        "age_seconds": round(age, 1) if age is not None else None,
        "max_age_seconds": max_age_seconds,
        "expected": expected, "state": state, "fresh": fresh,
    }


def due(source_key: str, seconds: int, *, now: float | None = None) -> bool:
    """次の取得に行くべきか。**取得タスク側から呼ばれるので expected は渡さない**
    (呼ばれている時点で取り込みの時間帯にいる)。"""
    return not source(source_key, now=now, max_age_seconds=seconds)["fresh"]
