"""生 RA レコードからのコーナー通過順位復元のテスト。純ロジックは CIセーフ。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, corner   # noqa: E402


def test_parse_passing_order_groups_and_marks():
    # 実データ例: コーナー3 の通過順位
    got = corner.parse_passing_order("(*5,6,8)(2,7)(1,4)3")
    assert got == {"5": 1, "6": 1, "8": 1,      # 横並びは同順位
                   "2": 2, "7": 2,
                   "1": 3, "4": 3,
                   "3": 4}
    # '*' は順位に無関係なので除去される
    assert corner.parse_passing_order("*7,3") == {"7": 1, "3": 2}


def test_parse_passing_order_gap_symbols():
    # '-' '=' は「大きな差」の記号。順位の区切りとしてのみ扱う
    got = corner.parse_passing_order("12,13(3,14)1-9(4,10)=8")
    assert got["12"] == 1 and got["13"] == 2
    assert got["3"] == 3 and got["14"] == 3       # 横並び
    assert got["1"] == 4 and got["9"] == 5
    assert got["4"] == 6 and got["10"] == 6
    assert got["8"] == 7
    # 順位は 1 から連番で増える
    assert min(got.values()) == 1


def test_parse_passing_order_whitespace_is_a_separator():
    """実データ回帰: 空白も区切り。除去すると馬番が連結して存在しない番号になる。

    実測で検出した不具合 — '=12   9' を空白除去すると '129' という馬番になっていた。
    """
    got = corner.parse_passing_order("(*14,7)(2,3,4)15(8,10)(5,11)1,16,6=13=12   9")
    assert "129" not in got and "1213" not in got     # 連結が起きない
    assert got["12"] < got["9"]                        # 12 が先、9 が最後
    assert got["9"] == max(got.values())
    assert set(got) == {"14", "7", "2", "3", "4", "15", "8", "10", "5", "11",
                        "1", "16", "6", "13", "12", "9"}

    got2 = corner.parse_passing_order("11(8,6,*4)-(7,12)9(11,14)(3,2)5-1=10   13")
    assert "1013" not in got2
    assert got2["10"] < got2["13"]
    # 括弧内の空白区切りも扱える
    assert corner.parse_passing_order("(1 2)3") == {"1": 1, "2": 1, "3": 2}


def test_parse_passing_order_edge_cases():
    assert corner.parse_passing_order("") == {}
    assert corner.parse_passing_order("   ") == {}
    assert corner.parse_passing_order("(1,2") == {"1": 1, "2": 1}   # 閉じ括弧欠落でも壊れない
    # 馬番はゼロ埋めを正規化する
    assert corner.parse_passing_order("05,03") == {"5": 1, "3": 2}


def _fake_ra(race_key: str, blocks: list[str]) -> bytes:
    """テスト用の RA レコードを組み立てる (キー位置とコーナーブロック位置を再現)。"""
    rec = bytearray(b" " * 1272)
    rec[0:2] = b"RA"
    rec[corner.RACE_KEY_SLICE] = race_key.encode("ascii")
    for i, s in enumerate(blocks):
        start = corner.CORNER_BLOCK_START + i * corner.CORNER_BLOCK_SIZE
        payload = s.encode("shift_jis").ljust(corner.CORNER_BLOCK_SIZE, b" ")
        rec[start:start + corner.CORNER_BLOCK_SIZE] = payload[:corner.CORNER_BLOCK_SIZE]
    return bytes(rec)


def test_parse_ra_corners_uses_declared_corner_number():
    # ブロックの並び順ではなく、先頭の「コーナー番号」で識別する
    rec = _fake_ra("2026071902011201", ["31(*5,6,8)(2,7)(1,4)3", "41(*5,6,8)(2,7,4)(1,3)"])
    got = corner.parse_ra_corners(rec)
    assert set(got) == {3, 4}
    assert got[3]["5"] == 1
    assert got[4]["1"] == 3 and got[4]["3"] == 3


def test_iter_ra_records_skips_nulls_and_short(tmp_path):
    # ブロック先頭2バイトは「コーナー番号+周回数」。以降が通過順位。
    rec1 = _fake_ra("2025010405010101", ["11" + "1,2(3,4)"])   # コーナー1・周回1
    rec2 = _fake_ra("2025010405010102", ["41" + "2,1"])        # コーナー4・周回1
    # 実ファイル同様にヌル埋め + CRLF 区切り、末尾にゴミ行
    blob = rec1 + b"\r\n" + b"\x00" + rec2 + b"\r\n" + b"\x00\r\n"
    f = tmp_path / "RASW_test.jvd"
    f.write_bytes(blob)

    got = dict(corner.iter_ra_records(f))
    assert set(got) == {"2025010405010101", "2025010405010102"}
    assert got["2025010405010101"][1] == {"1": 1, "2": 2, "3": 3, "4": 3}
    assert got["2025010405010102"][4] == {"2": 1, "1": 2}


def test_corner_positions_first_last_and_gain():
    index = {"R1": {"1": {"7": 10}, "4": {"7": 3}}}
    got = corner.corner_positions(index, "R1", "7")
    assert got["first"] == 10.0 and got["last"] == 3.0
    assert got["gain_first_last"] == 7.0        # 道中で 7 つ押し上げた
    # 該当馬なし / 該当レースなし
    assert corner.corner_positions(index, "R1", "99")["first"] is None
    assert corner.corner_positions(index, "ZZZ", "7")["last"] is None


def test_build_and_save_index_roundtrip(tmp_path):
    f = tmp_path / "RASW_a.jvd"
    f.write_bytes(_fake_ra("2025010405010101", ["11" + "1,2"]) + b"\r\n")
    idx = corner.build_corner_index(tmp_path)
    assert idx["2025010405010101"]["1"] == {"1": 1, "2": 2}
    out = corner.save_corner_index(idx, tmp_path / "cache" / "corner.json")
    assert corner.load_corner_index(out) == idx
    assert corner.load_corner_index(tmp_path / "missing.json") == {}


@pytest.mark.skipif(not (config.KEIBA_YOSOU_PATH / "data" / "raw" / "RACE").exists(),
                    reason="生 JV-Data が無い環境ではスキップ")
def test_real_raw_file_parses():
    """実ファイルが読めて、順位が 1 以上の連番として復元されること。"""
    raw = config.KEIBA_YOSOU_PATH / "data" / "raw" / "RACE"
    files = sorted(raw.glob("RASW*.jvd"))
    if not files:
        pytest.skip("RASW ファイルが無い")
    seen = 0
    for key, corners in corner.iter_ra_records(files[-1]):
        assert len(key) == 16 and key.isdigit()
        for cno, order in corners.items():
            assert 1 <= cno <= 4
            assert order and min(order.values()) == 1
            assert all(v >= 1 for v in order.values())
        seen += 1
        if seen >= 5:
            break
    assert seen > 0
