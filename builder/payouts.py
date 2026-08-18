"""確定払戻をJRA-VANのHRレコードから読み、購入点を精算する。

keiba.db の ``payouts`` は一部券種だけを保持しているため、ワイド・枠連・
馬単・三連単を含む画面表示と購入履歴の精算には、公式仕様のHR生レコードを
read-onlyで参照する。金額はすべて100円あたりの払戻円。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import betslip
from . import config


LABELS = {item["key"]: item["label"] for item in betslip.BET_TYPES}

# (券種, 0-based開始位置, 最大同着数, 1件の幅, 組番幅, 人気幅)
_AREAS = (
    ("tan", 102, 3, 13, 2, 2),
    ("fuku", 141, 5, 13, 2, 2),
    ("wakuren", 206, 3, 13, 2, 2),
    ("umaren", 245, 3, 16, 4, 3),
    ("wide", 293, 7, 16, 4, 3),
    ("umatan", 453, 6, 16, 4, 3),
    ("sanrenpuku", 549, 3, 18, 6, 3),
    ("sanrentan", 603, 6, 19, 6, 4),
)


def _digits(raw: bytes) -> int:
    text = raw.decode("ascii", "ignore").strip()
    return int(text) if text.isdigit() else 0


def _combo(kind: str, raw: str) -> tuple[int, ...]:
    if kind == "wakuren":
        return tuple(int(ch) for ch in raw if ch.isdigit() and ch != "0")
    if not raw.isdigit() or len(raw) % 2:
        return ()
    values = tuple(int(raw[i:i + 2]) for i in range(0, len(raw), 2))
    return values if all(values) else ()


def parse_hr(rec: bytes) -> list[dict]:
    """HR 1レコードを全券種の払戻一覧へ変換する。"""
    if not rec.startswith(b"HR") or len(rec) < 717:
        return []
    out = []
    for kind, start, count, width, combo_width, pop_width in _AREAS:
        for index in range(count):
            pos = start + index * width
            raw_combo = rec[pos:pos + combo_width].decode("ascii", "ignore").strip()
            combo = _combo(kind, raw_combo)
            payout = _digits(rec[pos + combo_width:pos + combo_width + 9])
            popularity = _digits(
                rec[pos + combo_width + 9:pos + combo_width + 9 + pop_width])
            if not combo or payout <= 0:
                continue
            spec = betslip.BY_KEY[kind]
            out.append({
                "type": kind,
                "label": LABELS[kind],
                "combo": list(combo),
                "text": betslip.combo_text(spec, [str(x) for x in combo]),
                "payout_yen_per_100": payout,
                "popularity": popularity or None,
            })
    return out


def _race_key(race: dict) -> str:
    return str(race.get("race_id") or "")


def _raw_dirs() -> tuple[Path, ...]:
    root = Path(config.KEIBA_YOSOU_PATH) / "data" / "raw"
    return (root / "0B12", root / "RACE")


# ---------------------------------------------------------------------------
# HR ファイルの索引キャッシュ
# ---------------------------------------------------------------------------
# 以前はレースごとに該当日の .jvd を全読みしていた。ライブ更新は25秒ごとに
# 全レースを回すので、**開発機で4日間に累計 2.1 TB を読み**、Windows Defender の
# リアルタイム検査を巻き込んで CPU を恒常的に消費していた (実測 142 MB/サイクル)。
#
# 直した点は2つ。
#   1. 読むのは **HR レコードを持つファイルだけ**。JV-Data はレコード種別ごとに
#      ファイルが分かれており (`HRSW…`)、`*{date}*` は票数 (H1/H6) やオッズ
#      (O1〜O6) まで巻き込んでいた。それらに HR は入っていない
#      → 1レースあたり 3.95 MB → 25 KB
#   2. ファイル1本を **1回だけ読んで全レース分を索引化**し、
#      (パス, mtime_ns, サイズ) を鍵にキャッシュする。同じ内容なら stat だけで
#      済み、**払戻が訂正されてファイルが更新されれば署名が変わって読み直す**
#      → 定常状態の読み取りはほぼゼロ
_INDEX_LIMIT = 16
_INDEX_CACHE: dict[tuple[str, int, int], dict[bytes, list[dict]]] = {}

# ディレクトリ走査の結果も日付単位でまとめる。`data/raw/RACE` は 2,912 ファイルあり、
# 同じ glob をレースごとに繰り返すと 36 レースで 340 ms かかっていた (実測)。
# TTL はライブ更新間隔 (25秒) より短くして、新しい HR が届いたら次の周回で拾う。
_LISTING_TTL_SECONDS = 5.0
_LISTING_CACHE: dict[str, tuple[float, list[tuple[Path, int, int]]]] = {}


def _listing(date: str) -> list[tuple[Path, int, int]]:
    """その開催日の HR 候補を (パス, mtime_ns, サイズ) で新しい順に返す。"""
    now = time.monotonic()
    got = _LISTING_CACHE.get(date)
    if got and now - got[0] < _LISTING_TTL_SECONDS:
        return got[1]
    found: list[Path] = []
    for directory in _raw_dirs():
        if not directory.exists():
            continue
        if directory.name == "0B12":
            # 速報払戻。レース単位のファイルなので日付でまとめて拾って後で絞る
            found.extend(directory.glob(f"0B12_{date}*_*.jvd"))
        else:
            found.extend(directory.glob(f"HR*{date}*.jvd"))
    out = []
    for path in dict.fromkeys(found):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((st.st_mtime_ns, st.st_size, path))
    out.sort(reverse=True)
    listing = [(path, mtime, size) for mtime, size, path in out]
    _LISTING_CACHE[date] = (now, listing)
    while len(_LISTING_CACHE) > _INDEX_LIMIT:
        _LISTING_CACHE.pop(next(iter(_LISTING_CACHE)))
    return listing


def _hr_candidates(date: str, track: str, race_num: str) -> list[tuple[Path, int, int]]:
    """HR レコードを持ちうるファイルだけを新しい順に返す。

    0B12 は1レース1ファイルなので、日付でまとめて取った一覧から
    そのレースのものだけを **メモリ上で** 絞る (再走査しない)。
    """
    prefix = f"0B12_{date}{track}{race_num}_"
    return [item for item in _listing(date)
            if not item[0].name.startswith("0B12_") or item[0].name.startswith(prefix)]


def _index_file(path: Path) -> dict[bytes, list[dict]]:
    """HR ファイルを1回読んで、レースキー → 払戻 の索引にする。"""
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    index: dict[bytes, list[dict]] = {}
    at = 0
    while True:
        at = data.find(b"HR", at)
        if at < 0:
            break
        rec = data[at:at + 719]
        # HRのレースキーは1-based 12～27文字 = 0-based 11:27。
        if len(rec) >= 719:
            got = parse_hr(rec)
            if got:
                index.setdefault(rec[11:27], got)
        at += 2
    return index


def _indexed(path: Path, mtime_ns: int, size: int) -> dict[bytes, list[dict]]:
    sig = (str(path), mtime_ns, size)
    got = _INDEX_CACHE.get(sig)
    if got is None:
        got = _index_file(path)
        _INDEX_CACHE[sig] = got
        while len(_INDEX_CACHE) > _INDEX_LIMIT:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    return got


def for_race(race: dict) -> list[dict]:
    """該当レースの最新HRを探す。見つからなければ空で誠実に劣化する。"""
    key = _race_key(race)
    if len(key) != 16:
        return []
    key_bytes = key.encode("ascii")
    date, track, race_num = key[:8], key[8:10], key[-2:]
    for path, mtime_ns, size in _hr_candidates(date, track, race_num):
        got = _indexed(path, mtime_ns, size).get(key_bytes)
        if got:
            return got
    return []


def settle(items: list[dict], payouts: list[dict]) -> dict:
    """保存済み購入点を精算し、投資・払戻・的中点数を返す。"""
    table = {
        (p["type"], tuple(int(x) for x in p["combo"])): int(p["payout_yen_per_100"])
        for p in payouts
    }
    invested = sum(int(item.get("amount_yen") or 0) for item in items)
    returned = 0
    hits = []
    for item in items:
        kind = str(item.get("type") or "")
        combo = tuple(int(x) for x in (item.get("combo") or []))
        if kind in ("wakuren", "umaren", "wide", "sanrenpuku"):
            combo = tuple(sorted(combo))
        payout = table.get((kind, combo), 0)
        if payout <= 0:
            continue
        amount = int(item.get("amount_yen") or 0)
        got = payout * amount // 100
        returned += got
        hits.append({**item, "payout_yen_per_100": payout, "return_yen": got})
    return {"invested_yen": invested, "returned_yen": returned,
            "profit_yen": returned - invested, "hit_points": len(hits), "hits": hits}
