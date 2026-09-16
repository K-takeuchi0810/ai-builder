"""確定払戻の読み取りと精算。**実資金の精算に使うので数字を機械的に固定する。**

I/O についても回帰テストを置く。以前はレースごとに該当日の `.jvd` を全読みしており、
25秒ごとのライブ更新で **4日間に累計 2.1 TB** を読んでいた (実測 142 MB/サイクル)。
Windows Defender のリアルタイム検査を巻き込んで CPU を恒常的に消費していたので、
「読まないこと」もテストで押さえる。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import payouts as po   # noqa: E402

RACE_KEY = "2026072601010201"


def _hr_record(key: str, entries: dict) -> bytes:
    """HR 1レコードを組み立てる (仕様の位置に値を置く)。

    entries: {券種: [(組番文字列, 払戻円/100, 人気), ...]}
    """
    rec = bytearray(b" " * 719)
    rec[0:2] = b"HR"
    rec[11:27] = key.encode("ascii")           # 0-based 11:27 がレースキー
    for kind, start, count, width, combo_w, pop_w in po._AREAS:
        for i, (combo, payout, pop) in enumerate(entries.get(kind, [])[:count]):
            pos = start + i * width
            rec[pos:pos + combo_w] = combo.rjust(combo_w, "0").encode("ascii")
            rec[pos + combo_w:pos + combo_w + 9] = str(payout).rjust(9, "0").encode("ascii")
            rec[pos + combo_w + 9:pos + combo_w + 9 + pop_w] = \
                str(pop).rjust(pop_w, "0").encode("ascii")
    return bytes(rec)


@pytest.fixture
def raw(tmp_path, monkeypatch):
    """`data/raw/{0B12,RACE}` を模した一時ディレクトリに差し替える。"""
    root = tmp_path / "data" / "raw"
    (root / "0B12").mkdir(parents=True)
    (root / "RACE").mkdir(parents=True)
    monkeypatch.setattr(po, "_raw_dirs", lambda: (root / "0B12", root / "RACE"))
    po._INDEX_CACHE.clear()
    po._LISTING_CACHE.clear()
    yield root
    po._INDEX_CACHE.clear()
    po._LISTING_CACHE.clear()


class _Counter:
    """read_bytes の呼び出しとバイト数を数える。"""

    def __init__(self, monkeypatch):
        self.files = []
        self.total = 0
        orig = Path.read_bytes

        def spy(inner_self):
            data = orig(inner_self)
            self.files.append(inner_self.name)
            self.total += len(data)
            return data

        monkeypatch.setattr(Path, "read_bytes", spy)

    def reset(self):
        self.files.clear()
        self.total = 0


# ---------------------------------------------------------------------------
# HR レコードの解釈
# ---------------------------------------------------------------------------
def test_parse_hr_reads_every_bet_type():
    rec = _hr_record(RACE_KEY, {
        "tan": [("10", 180, 1)],
        "fuku": [("10", 110, 1), ("02", 820, 9)],
        "wakuren": [("27", 4020, 14)],
        "umaren": [("0210", 9130, 30)],
        "wide": [("0210", 2450, 26)],
        "umatan": [("1002", 15600, 52)],
        "sanrenpuku": [("010210", 30150, 88)],
        "sanrentan": [("100201", 187540, 512)],
    })
    parsed = po.parse_hr(rec)
    got = {x["type"]: x for x in parsed}
    assert got["tan"]["combo"] == [10] and got["tan"]["payout_yen_per_100"] == 180
    assert got["wakuren"]["text"] == "2-7"
    assert got["umaren"]["text"] == "2-10"
    assert got["umatan"]["text"] == "10→2"          # 順序が残る
    assert got["sanrenpuku"]["text"] == "1-2-10"
    assert got["sanrentan"]["text"] == "10→2→1"
    assert got["sanrentan"]["popularity"] == 512
    # 複勝は同着分が全部出る
    assert len([x for x in parsed if x["type"] == "fuku"]) == 2


def test_parse_hr_rejects_a_record_that_is_not_hr():
    assert po.parse_hr(b"RA" + b" " * 800) == []
    assert po.parse_hr(b"HR" + b" " * 10) == []      # 短すぎる


def test_parse_hr_skips_empty_slots():
    """組番が空・払戻0の枠は落とす (捏造しない)。"""
    assert po.parse_hr(_hr_record(RACE_KEY, {"tan": [("10", 0, 1)]})) == []


# ---------------------------------------------------------------------------
# ファイルからの取得
# ---------------------------------------------------------------------------
def test_for_race_finds_the_payout(raw):
    name = "HRSW" + RACE_KEY[:8] + "20260726120000.jvd"
    (raw / "RACE" / name).write_bytes(
        _hr_record(RACE_KEY, {"tan": [("10", 180, 1)]}))
    assert [x["text"] for x in po.for_race({"race_id": RACE_KEY})] == ["10"]


def test_for_race_degrades_to_empty(raw):
    """見つからなければ空。**適当な数字を返さない。**"""
    assert po.for_race({"race_id": RACE_KEY}) == []
    assert po.for_race({"race_id": "short"}) == []
    assert po.for_race({}) == []


def test_only_hr_files_are_read(raw, monkeypatch):
    """HR を持たないファイルは読まないこと。

    以前は `*{date}*.jvd` で票数 (H1/H6) やオッズ (O1〜O6) まで巻き込み、
    1レースあたり 3.95 MB 読んでいた。それらのファイルに HR は入っていない。
    """
    date = RACE_KEY[:8]
    hr_name = "HRSW" + date + "20260726120000.jvd"
    (raw / "RACE" / hr_name).write_bytes(
        _hr_record(RACE_KEY, {"tan": [("10", 180, 1)]}))
    # HR を持たない大きなファイルを並べる (中身にたまたま "HR" を含ませる)
    noise = b"O1" + b"HR" + b"x" * 200_000
    for prefix in ("O1SW", "O6SW", "H1SW", "SESW"):
        (raw / "RACE" / (prefix + date + "20260726130000.jvd")).write_bytes(noise)

    counter = _Counter(monkeypatch)
    got = po.for_race({"race_id": RACE_KEY})
    assert [x["text"] for x in got] == ["10"]
    assert counter.files == [hr_name], counter.files
    assert counter.total < 10_000, counter.total


def test_repeated_calls_do_not_read_again(raw, monkeypatch):
    """同じ内容なら2回目以降は読み直さない。

    ライブ更新は25秒ごとに全レースを回るので、ここが効かないと
    ファイル1本を1日あたり数千回読むことになる。
    """
    date = RACE_KEY[:8]
    keys = [date + "0101" + "%02d%02d" % (i, i) for i in range(1, 13)]
    payload = b"".join(_hr_record(k, {"tan": [("10", 180 + i, 1)]})
                       for i, k in enumerate(keys))
    (raw / "RACE" / ("HRSW" + date + "20260726120000.jvd")).write_bytes(payload)

    counter = _Counter(monkeypatch)
    first = [po.for_race({"race_id": k}) for k in keys]
    assert all(first), "1周目で全レース取れていない"
    # 1本のファイルを1回だけ読んで全レース分を索引化する
    assert len(counter.files) == 1, counter.files
    counter.reset()
    second = [po.for_race({"race_id": k}) for k in keys]
    assert second == first
    assert counter.files == [], "2周目でファイルを読み直している"
    assert counter.total == 0


def test_a_corrected_file_is_picked_up(raw):
    """払戻が訂正されてファイルが更新されたら読み直すこと。

    降着・審議で払戻が変わることがある。キャッシュが効きすぎて古い払戻で
    精算し続けると、**実資金の集計が誤る**。
    """
    date = RACE_KEY[:8]
    path = raw / "RACE" / ("HRSW" + date + "20260726120000.jvd")
    path.write_bytes(_hr_record(RACE_KEY, {"tan": [("10", 180, 1)]}))
    assert po.for_race({"race_id": RACE_KEY})[0]["payout_yen_per_100"] == 180

    # 同じパスを別内容で上書きし、mtime を進める
    path.write_bytes(_hr_record(RACE_KEY, {"tan": [("10", 250, 1)]}))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    po._LISTING_CACHE.clear()          # 一覧の TTL 経過を再現
    assert po.for_race({"race_id": RACE_KEY})[0]["payout_yen_per_100"] == 250


def test_the_newest_file_wins(raw):
    """速報 (0B12) と成績 (HR) が両方あれば新しい方を使う。"""
    date = RACE_KEY[:8]
    old = raw / "RACE" / ("HRSW" + date + "20260726120000.jvd")
    old.write_bytes(_hr_record(RACE_KEY, {"tan": [("10", 180, 1)]}))
    # 0B12 の名前は 0B12_{date}{track}{race_num}_… (track=01, race_num=01)
    new = raw / "0B12" / ("0B12_" + date + "0101_20260726130000.jvd")
    new.write_bytes(_hr_record(RACE_KEY, {"tan": [("10", 999, 1)]}))
    st = old.stat()
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    po._LISTING_CACHE.clear()
    assert po.for_race({"race_id": RACE_KEY})[0]["payout_yen_per_100"] == 999


def test_the_realtime_file_of_another_race_is_not_used(raw):
    """0B12 は1レース1ファイル。別レースのファイルを混ぜないこと。"""
    date = RACE_KEY[:8]
    other = raw / "0B12" / ("0B12_" + date + "0199_20260726130000.jvd")
    other.write_bytes(_hr_record(RACE_KEY, {"tan": [("10", 999, 1)]}))
    assert po.for_race({"race_id": RACE_KEY}) == []


def test_the_caches_are_bounded(raw):
    """日をまたいで常駐しても索引と一覧が無限に増えないこと。"""
    for day in range(1, po._INDEX_LIMIT + 6):
        date = "202607%02d" % day
        key = date + "0101" + "0101"
        (raw / "RACE" / ("HRSW" + date + "20260726120000.jvd")).write_bytes(
            _hr_record(key, {"tan": [("10", 180, 1)]}))
        po._LISTING_CACHE.clear()
        po.for_race({"race_id": key})
    assert len(po._INDEX_CACHE) <= po._INDEX_LIMIT, len(po._INDEX_CACHE)
    assert len(po._LISTING_CACHE) <= po._INDEX_LIMIT, len(po._LISTING_CACHE)


# ---------------------------------------------------------------------------
# 精算
# ---------------------------------------------------------------------------
def test_settle_pays_only_the_matching_points():
    payouts = [
        {"type": "tan", "combo": [10], "payout_yen_per_100": 180},
        {"type": "umaren", "combo": [2, 10], "payout_yen_per_100": 9130},
    ]
    items = [
        {"type": "tan", "combo": [10], "amount_yen": 300},        # 的中
        {"type": "tan", "combo": [3], "amount_yen": 100},         # 外れ
        {"type": "umaren", "combo": [10, 2], "amount_yen": 100},  # 順不同で的中
    ]
    got = po.settle(items, payouts)
    assert got["invested_yen"] == 500
    assert got["returned_yen"] == 180 * 3 + 9130          # 540 + 9130
    assert got["profit_yen"] == got["returned_yen"] - 500
    assert got["hit_points"] == 2


def test_settle_respects_the_order_for_ordered_types():
    """馬単・三連単は順序が違えば別の馬券。**並べ替えて的中にしない。**"""
    payouts = [{"type": "umatan", "combo": [10, 2], "payout_yen_per_100": 15600}]
    hit = po.settle([{"type": "umatan", "combo": [10, 2], "amount_yen": 100}], payouts)
    miss = po.settle([{"type": "umatan", "combo": [2, 10], "amount_yen": 100}], payouts)
    assert hit["returned_yen"] == 15600
    assert miss["returned_yen"] == 0


def test_settle_with_no_payouts_returns_the_investment_as_loss():
    got = po.settle([{"type": "tan", "combo": [10], "amount_yen": 100}], [])
    assert got == {"invested_yen": 100, "returned_yen": 0, "profit_yen": -100,
                   "hit_points": 0, "hits": []}
