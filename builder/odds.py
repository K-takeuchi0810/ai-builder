"""選択された買い目に、取得済みの発走前オッズを付ける。

単勝は日次行列の各馬にある最新値を使う。馬連・ワイド・馬単・三連複・三連単は
keiba-yosou の ``exotic_odds`` を read-only で参照する。発走前画面に確定オッズ
(``data_div=5``) を混ぜると PIT 違反になるため、未確定レースでは必ず除外する。

値が無いときは推測・代用せず ``available=False`` を返す。複勝・枠連は現在のDBに
発走前オッズの保存先がないため、常に未発表扱いになる。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

_DB_KIND = {
    "umaren": "quinella",
    "wide": "wide",
    "umatan": "exacta",
    "sanrenpuku": "trio",
    "sanrentan": "trifecta",
}


def _unavailable() -> dict:
    return {"available": False, "status": "not_released"}


def _combo_key(kind: str, combo: list[str] | tuple[str, ...]) -> str:
    nums = [int(x) for x in combo]
    if kind in ("umaren", "wide", "sanrenpuku"):
        nums.sort()
    return "".join(f"{n:02d}" for n in nums)


def _finished(race: dict) -> bool:
    return any(h.get("order") == 1 for h in race.get("horses", []))


def _race_keys(race: dict) -> tuple[str, str, str, str, str, str] | None:
    seg = race.get("seg") or {}
    raw_date = str(race.get("date") or "")
    race_id = str(race.get("race_id") or "")
    # 日次行列の seg は予想条件だけに絞られており、kaiji/nichiji を持たない版がある。
    # JRA の16桁レースID (年月日+場+回+日+R) はこの2項目の正本なので、欠けた時だけ
    # そこから補う。これをしないとDBに速報オッズが入っていても全点が未発表になる。
    id_matches = (len(race_id) == 16 and race_id.isdigit()
                  and race_id[:8] == raw_date)
    track = str(seg.get("track") or (race_id[8:10] if id_matches else ""))
    kaiji = str(seg.get("kaiji") or (race_id[10:12] if id_matches else ""))
    nichiji = str(seg.get("nichiji") or (race_id[12:14] if id_matches else ""))
    race_num = str(race.get("race_num") or (race_id[14:16] if id_matches else "")).zfill(2)
    keys = (raw_date[:4], raw_date[4:], track, kaiji, nichiji, race_num)
    if len(raw_date) != 8 or not all(keys):
        return None
    return keys


def _exotic_rows(race: dict, db_path: Path) -> dict[tuple[str, str], dict]:
    keys = _race_keys(race)
    if not keys:
        return {}
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT bet_type, combo, odds_low, odds_high, popularity, "
                "data_div, announced_time FROM exotic_odds "
                "WHERE race_year=? AND race_month_day=? AND track_code=? "
                "AND kaiji=? AND nichiji=? AND race_num=?",
                keys,
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {}

    out = {}
    race_finished = _finished(race)
    for row in rows:
        # data_div=5 はレース後の確定値。発走前には絶対に見せない。
        if not race_finished and str(row["data_div"] or "") in ("4", "5"):
            continue
        low = int(row["odds_low"] or 0)
        high = int(row["odds_high"] or 0)
        if low <= 0:
            continue
        out[(str(row["bet_type"]), str(row["combo"]))] = {
            "available": True,
            "status": "final" if str(row["data_div"] or "") == "5" else "provisional",
            "low": low / 10.0,
            "high": (high / 10.0) if high > 0 else None,
            "popularity": int(row["popularity"] or 0) or None,
            "as_of": str(row["announced_time"] or "") or None,
        }
    return out


def _o1_as_of(race_key: str, announced: str) -> str | None:
    """O1の発表時刻(MMDDHHMM)を、レース年を補ったISO文字列へ変換する。"""
    if len(race_key) < 4 or len(announced) != 8 or not announced.isdigit():
        return None
    try:
        value = datetime.strptime(race_key[:4] + announced, "%Y%m%d%H%M")
    except ValueError:
        return None
    return value.astimezone().isoformat(timespec="seconds")


def _raw_o1(race: dict) -> dict[tuple[str, str], dict]:
    """JV-Data O1から、DB未収録の複勝・枠連オッズを直接読む。

    既存DBは単勝のみを保存していたため、取得済みの0B30/0B31原本を
    read-onlyで参照する。ファイル名はレースキーを含むため全件走査しない。
    """
    keys = _race_keys(race)
    if not keys:
        return {}
    race_key = "".join(keys)
    raw_root = config.KEIBA_YOSOU_PATH / "data" / "raw"
    candidates: list[Path] = []
    for option in ("0B30", "0B31"):
        candidates.extend((raw_root / option).glob(f"{option}_{race_key}_*.jvd"))
    if not candidates:
        return {}

    race_finished = _finished(race)
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            record = path.read_bytes()
        except OSError:
            continue
        if len(record) < 962 or record[:2] != b"O1":
            continue
        try:
            text = record[:962].decode("cp932")
        except UnicodeDecodeError:
            continue
        if text[11:27] != race_key:
            continue
        data_div = text[2:3]
        # 4/5はレース後の確定値。発走前画面へ混ぜると未来情報になる。
        if not race_finished and data_div in ("4", "5"):
            continue
        status = "final" if data_div in ("4", "5") else "provisional"
        as_of = _o1_as_of(race_key, text[27:35])
        out: dict[tuple[str, str], dict] = {}

        # 複勝: 馬番2 + 最低4 + 最高4 + 人気2 (28件)
        for index in range(28):
            entry = text[267 + index * 12:267 + (index + 1) * 12]
            horse_num, low_raw, high_raw, pop_raw = (
                entry[:2], entry[2:6], entry[6:10], entry[10:12]
            )
            if not horse_num.strip() or not low_raw.isdigit() or int(low_raw) <= 0:
                continue
            out[("place", horse_num)] = {
                "available": True,
                "status": status,
                "low": int(low_raw) / 10.0,
                "high": int(high_raw) / 10.0 if high_raw.isdigit() and int(high_raw) > 0 else None,
                "popularity": int(pop_raw) if pop_raw.isdigit() and int(pop_raw) > 0 else None,
                "as_of": as_of,
            }

        # 枠連: 組番2 + オッズ5 + 人気2 (36件)
        for index in range(36):
            entry = text[603 + index * 9:603 + (index + 1) * 9]
            combo, odds_raw, pop_raw = entry[:2], entry[2:7], entry[7:9]
            if not combo.strip() or not odds_raw.isdigit() or int(odds_raw) <= 0:
                continue
            out[("bracket_quinella", combo)] = {
                "available": True,
                "status": status,
                "low": int(odds_raw) / 10.0,
                "high": None,
                "popularity": int(pop_raw) if pop_raw.isdigit() and int(pop_raw) > 0 else None,
                "as_of": as_of,
            }
        return out
    return {}


def _win_rows(race: dict, db_path: Path) -> dict[str, dict]:
    """最新DB値を直接読む。日次キャッシュの再構築を待たず更新ボタンへ反映する。"""
    keys = _race_keys(race)
    if not keys:
        return {}
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT horse_num, win_odds, win_popularity, odds_fetched_at "
                "FROM horse_races WHERE race_year=? AND race_month_day=? "
                "AND track_code=? AND kaiji=? AND nichiji=? AND race_num=?",
                keys,
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {}
    out = {}
    race_finished = _finished(race)
    for row in rows:
        raw = int(row["win_odds"] or 0)
        stamp = str(row["odds_fetched_at"] or "") or None
        # 発走前は時刻のあるPITスナップショットだけを許可する。
        if raw <= 0 or (not race_finished and not stamp):
            continue
        out[str(row["horse_num"] or "").zfill(2)] = {
            "available": True,
            "status": "final" if race_finished and not stamp else "provisional",
            "low": raw / 10.0, "high": None,
            "popularity": int(row["win_popularity"] or 0) or None,
            "as_of": stamp,
        }
    return out


def attach(race: dict, slip: list[dict], *, db_path: Path | None = None) -> None:
    """slip の各点と同じ並びで ``odds`` を付ける（in-place）。"""
    path = db_path or config.KEIBA_DB_PATH
    exotic = _exotic_rows(race, path)
    o1 = _raw_o1(race)
    current_win = _win_rows(race, path)
    horses = {str(h.get("num") or "").zfill(2): h for h in race.get("horses", [])}
    for entry in slip:
        kind = entry["key"]
        values = []
        for combo in entry.get("combos", []):
            if kind == "tan":
                horse = horses.get(str(combo[0]).zfill(2))
                db_value = current_win.get(str(combo[0]).zfill(2))
                if db_value:
                    values.append(db_value)
                    continue
                value = horse.get("odds") if horse else None
                if isinstance(value, (int, float)) and value > 0:
                    values.append({
                        "available": True, "status": "provisional",
                        "low": float(value), "high": None,
                        "popularity": int(horse.get("pop") or 0) or None,
                        "as_of": race.get("odds_as_of"),
                    })
                else:
                    values.append(_unavailable())
                continue
            if kind == "fuku":
                values.append(o1.get(("place", str(combo[0]).zfill(2)), _unavailable()))
                continue
            if kind == "wakuren":
                frame_key = "".join(str(int(number)) for number in sorted(combo, key=int))
                values.append(o1.get(("bracket_quinella", frame_key), _unavailable()))
                continue
            db_kind = _DB_KIND.get(kind)
            values.append(exotic.get((db_kind, _combo_key(kind, combo)), _unavailable())
                          if db_kind else _unavailable())
        entry["odds"] = values
