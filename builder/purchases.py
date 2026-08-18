"""利用者ごとの買い目・金額・購入確認を永続保存する。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from . import labels
from . import payouts


_LOCK = threading.RLock()
MIN_USER_RACES_FOR_RANK = 5


def _path() -> Path:
    return Path(config.PRESET_WEIGHTS_PATH).parent / "purchases.json"


def _owner(owner_id: str | None) -> str:
    return owner_id or "local"


def _load() -> dict:
    path = _path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def recorded_dates(owner_id: str | None = None, *, all_users: bool = False) -> list[str]:
    """保存済み購入記録の日付を昇順で返す。"""
    with _LOCK:
        data = _load()
    sources = data.values() if all_users else [data.get(_owner(owner_id)) or []]
    return sorted({str(row.get("date") or "") for rows in sources for row in rows or []
                   if not row.get("deleted_at")
                   and len(str(row.get("date") or "")) == 8})


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _start_at(race: dict) -> int | None:
    date = str(race.get("date") or "")
    raw_time = str(race.get("start_time") or "").replace(":", "")[:4]
    if len(date) != 8 or len(raw_time) != 4 or not (date + raw_time).isdigit():
        return None
    try:
        dt = datetime.strptime(date + raw_time, "%Y%m%d%H%M").replace(
            tzinfo=ZoneInfo("Asia/Tokyo"))
    except ValueError:
        return None
    return int(dt.timestamp())


def record(owner_id: str | None, *, race: dict, config_id: str | None,
           items: list[dict], prediction: list[dict] | None = None,
           config_name: str | None = None, config_snapshot: dict | None = None,
           status: str = "qr_created", source: str = "smappy_qr",
           now: int | None = None) -> dict:
    """QR作成内容を購入確認前として保存する。同一内容の連打は1件にまとめる。"""
    created = int(time.time() if now is None else now)
    race_id = str(race.get("race_id") or "")
    normalized = [{"type": str(x.get("type") or ""),
                   "label": str(x.get("label") or ""),
                   "combo": [int(v) for v in (x.get("combo") or [])],
                   "text": str(x.get("text") or ""),
                   "amount_yen": int(x.get("amount_yen") or 0)} for x in items]
    with _LOCK:
        data = _load()
        rows = data.setdefault(_owner(owner_id), [])
        recent = next((r for r in reversed(rows)
                       if not r.get("deleted_at")
                       if r.get("race_id") == race_id and r.get("items") == normalized
                       and created - int(r.get("created_at") or 0) < 300), None)
        if recent:
            # QR作成直後に「購入済みとして保存」を押した場合も重複を作らない。
            if status == "purchased" and recent.get("status") != "purchased":
                recent["status"] = "purchased"
                recent["confirmed_at"] = created
                recent["source"] = source
                _save(data)
            return recent
        start_at = _start_at(race)
        entry = {
            "id": uuid.uuid4().hex,
            "date": str(race.get("date") or ""),
            "race_id": race_id,
            "race_num": str(race.get("race_num") or ""),
            "race_name": race.get("race_title") or race.get("race_class")
                         or race.get("race_name") or "",
            "track": (race.get("seg") or {}).get("track"),
            "start_time": race.get("start_time") or "",
            "config_id": config_id,
            "config_name": config_name,
            "config_snapshot": config_snapshot,
            "created_at": created,
            "start_at": start_at,
            "registered_before_start": bool(start_at and created < start_at),
            "confirmed_at": created if status == "purchased" else None,
            "status": status,
            "source": source,
            "deleted_at": None,
            "items": normalized,
            "prediction": prediction or [],
        }
        rows.append(entry)
        # 利用者ごとに直近90日分を残す。
        keep_dates = sorted({str(r.get("date") or "") for r in rows})[-90:]
        data[_owner(owner_id)] = [r for r in rows if str(r.get("date") or "") in keep_dates]
        _save(data)
    return entry


def _eligible(row: dict, race: dict | None) -> bool:
    if "registered_before_start" in row:
        return bool(row.get("registered_before_start"))
    start_at = int(row.get("start_at") or 0) or (_start_at(race or {}) or 0)
    return bool(start_at and int(row.get("created_at") or 0) < start_at)


def _factor_labels(row: dict, owner_key: str) -> list[str]:
    from . import configs as cf
    from . import labels as lb

    cfg = row.get("config_snapshot")
    if not cfg and row.get("config_id"):
        owner_id = None if owner_key == "local" else owner_key
        got = cf.get_config(str(row["config_id"]), owner_id=owner_id)
        cfg = got.get("config") if got else None
    if not cfg:
        return []
    labels = [lb.column_label(str(key)) for key in cfg.get("step1") or []]
    labels.extend(lb.column_label(str(cell.get("metric") or ""), cell.get("match"),
                                  cell.get("lookback"))
                  for cell in cfg.get("step2") or [])
    return labels


def _race_display(race: dict | None, row: dict) -> dict:
    """購入履歴を競馬場・発走時刻つきの表示用情報へ揃える。"""
    race = race or {}
    seg = race.get("seg") or {}
    track = str(seg.get("track") or row.get("track") or "")
    return {
        "track": track,
        "track_label": labels.value_label("track", track) if track else "",
        "start_time": race.get("start_time") or row.get("start_time") or "",
    }


def user_leaderboard(date: str, races: list[dict], names: dict[str, str]) -> dict:
    """発走前にQR登録された最終買い目を、利用者単位で集計する。

    実購入を外部照合できないため「購入成績」と断定せず、発走前登録成績として扱う。
    同じレースでQRを作り直した場合は、発走前の最後の1件だけを採用する。
    """
    return user_leaderboard_for_dates([str(date)], races, names)


def user_leaderboard_for_dates(dates: list[str], races: list[dict],
                               names: dict[str, str]) -> dict:
    """指定期間の発走前登録成績を利用者単位で集計する。"""
    date_set = {str(value) for value in dates}
    with _LOCK:
        data = _load()
    by_race = {str(r.get("race_id") or ""): r for r in races}
    entries = []
    for owner_key, raw_rows in data.items():
        latest: dict[str, dict] = {}
        for raw in raw_rows or []:
            if raw.get("deleted_at"):
                continue
            if str(raw.get("date") or "") not in date_set:
                continue
            race = by_race.get(str(raw.get("race_id") or ""))
            if not _eligible(raw, race):
                continue
            rid = str(raw.get("race_id") or "")
            if rid not in latest or int(raw.get("created_at") or 0) > int(
                    latest[rid].get("created_at") or 0):
                latest[rid] = dict(raw)
        if not latest:
            continue

        registered_yen = settled_invested = returned = hit_races = settled = 0
        details = []
        for row in sorted(latest.values(), key=lambda item: int(item.get("created_at") or 0)):
            race = by_race.get(str(row.get("race_id") or ""))
            finished = bool(race and any(h.get("order") == 1 for h in race.get("horses", [])))
            payout_rows = payouts.for_race(race) if finished and race else []
            settlement = payouts.settle(row.get("items") or [], payout_rows)
            display = _race_display(race, row)
            registered_yen += settlement["invested_yen"]
            if finished:
                settled += 1
                settled_invested += settlement["invested_yen"]
                returned += settlement["returned_yen"]
                hit_races += int(settlement["hit_points"] > 0)
            details.append({
                "date": row.get("date"),
                "race_id": row.get("race_id"), "race_num": row.get("race_num"),
                "race_name": row.get("race_name"), "created_at": row.get("created_at"),
                **display,
                "config_name": row.get("config_name"),
                "factors": _factor_labels(row, owner_key),
                "items": row.get("items") or [], "finished": finished,
                "settlement": settlement,
            })
        # 未発走レースの登録額を分母に入れると、開催途中だけ回収率が不当に
        # 下がるため、回収率・収支は確定済みレースだけで計算する。
        roi = returned / settled_invested if settled_invested > 0 else None
        entries.append({
            "display_name": names.get(owner_key) or ("管理者" if owner_key == "role:admin"
                                                       else "利用者"),
            "registered_races": len(latest), "settled_races": settled,
            "registered_yen": registered_yen,
            "invested_yen": settled_invested, "returned_yen": returned,
            "profit_yen": returned - settled_invested, "hit_races": hit_races,
            "hit_rate": hit_races / settled if settled else None,
            "roi": roi, "provisional": settled < MIN_USER_RACES_FOR_RANK,
            "rank": None, "details": details,
        })

    qualified = [entry for entry in entries if not entry["provisional"] and entry["roi"] is not None]
    qualified.sort(key=lambda entry: (entry["roi"], entry["hit_rate"] or 0,
                                      entry["settled_races"]), reverse=True)
    previous = None
    rank = 0
    for index, entry in enumerate(qualified, 1):
        key = (round(entry["roi"], 6), round(entry["hit_rate"] or 0, 6))
        if key != previous:
            rank = index
            previous = key
        entry["rank"] = rank
    entries.sort(key=lambda entry: (entry["rank"] is None, entry["rank"] or 9999,
                                    -(entry["settled_races"] or 0), entry["display_name"]))
    return {"date": max(date_set) if date_set else "", "dates": sorted(date_set),
            "entries": entries,
            "minimum_races_for_rank": MIN_USER_RACES_FOR_RANK,
            "updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes"),
            "note": "発走前にQRへ登録した最後の買い目を集計しています。実購入の外部照合ではありません。"}


def confirm(owner_id: str | None, purchase_id: str, *, confirmed: bool = True,
            now: int | None = None) -> dict | None:
    with _LOCK:
        data = _load()
        rows = data.get(_owner(owner_id)) or []
        got = next((r for r in rows if r.get("id") == purchase_id
                    and not r.get("deleted_at")), None)
        if not got:
            return None
        got["status"] = "purchased" if confirmed else "qr_created"
        got["confirmed_at"] = int(time.time() if now is None else now) if confirmed else None
        _save(data)
        return got


def delete_record(owner_id: str | None, purchase_id: str, *,
                  now: int | None = None) -> dict | None:
    """自分の購入記録を論理削除する。監査・誤操作復旧用に実体は残す。"""
    with _LOCK:
        data = _load()
        rows = data.get(_owner(owner_id)) or []
        got = next((r for r in rows if r.get("id") == purchase_id
                    and not r.get("deleted_at")), None)
        if not got:
            return None
        got["deleted_at"] = int(time.time() if now is None else now)
        _save(data)
        return {"ok": True, "purchase_id": purchase_id,
                "deleted_at": got["deleted_at"]}


def list_for_date(owner_id: str | None, date: str, races: list[dict]) -> dict:
    return list_for_dates(owner_id, [str(date)], races)


def list_for_dates(owner_id: str | None, dates: list[str], races: list[dict]) -> dict:
    """指定期間の購入記録・払戻・回収率を集計する。"""
    date_set = {str(value) for value in dates}
    with _LOCK:
        rows = [dict(r) for r in (_load().get(_owner(owner_id)) or [])
                if str(r.get("date") or "") in date_set and not r.get("deleted_at")]
    by_race = {str(r.get("race_id")): r for r in races}
    purchased = invested = returned = hit_points = finished = 0
    unconfirmed_yen = unconfirmed_races = 0
    for row in rows:
        race = by_race.get(str(row.get("race_id") or ""))
        row.update(_race_display(race, row))
        row["finished"] = bool(race and any(h.get("order") == 1
                                            for h in race.get("horses", [])))
        row["payouts"] = payouts.for_race(race) if row["finished"] and race else []
        row["settlement"] = payouts.settle(row.get("items") or [], row["payouts"])
        if row.get("status") == "purchased":
            purchased += row["settlement"]["invested_yen"]
            if row["finished"]:
                finished += 1
                invested += row["settlement"]["invested_yen"]
                returned += row["settlement"]["returned_yen"]
                hit_points += row["settlement"]["hit_points"]
        else:
            unconfirmed_races += 1
            unconfirmed_yen += row["settlement"]["invested_yen"]
    return {"date": max(date_set) if date_set else "", "dates": sorted(date_set),
            "entries": rows, "confirmed_races":
            sum(1 for r in rows if r.get("status") == "purchased"),
            "unconfirmed_races": unconfirmed_races,
            "unconfirmed_yen": unconfirmed_yen,
            "settled_races": finished, "purchased_yen": purchased,
            "invested_yen": invested,
            "returned_yen": returned, "profit_yen": returned - invested,
            "hit_points": hit_points,
            "updated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes"),
            "roi": (returned / invested) if invested > 0 and finished > 0 else None}
