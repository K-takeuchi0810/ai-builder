"""生 SE レコードからの賞金復元のテスト。純ロジックは CIセーフ。"""

from __future__ import annotations

import os
import statistics as st
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, prize   # noqa: E402


def _fake_se(race_key: str, horse_num: int, honsho_hyaku: int, shutoku_hyaku: int = 0,
             confirmed_order: int = 1) -> bytes:
    """テスト用 SE レコード (キー・馬番・確定着順・賞金の位置を再現)。"""
    rec = bytearray(b"0" * 556)
    rec[0:2] = b"SE"
    rec[prize.RACE_KEY_SLICE] = race_key.encode("ascii")
    rec[prize.HORSE_NUM_SLICE] = f"{horse_num:02d}".encode("ascii")
    rec[334:336] = f"{confirmed_order:02d}".encode("ascii")          # 1-based 335,2
    rec[prize.HONSHO_POS - 1:prize.HONSHO_POS - 1 + 8] = f"{honsho_hyaku:08d}".encode()
    rec[prize.SHUTOKU_POS - 1:prize.SHUTOKU_POS - 1 + 8] = f"{shutoku_hyaku:08d}".encode()
    return bytes(rec)


def test_parse_se_prize_unit_is_100yen():
    """賞金の単位は百円。円に換算して返すこと。"""
    rec = _fake_se("2025010405010101", 7, honsho_hyaku=82000, shutoku_hyaku=1500)
    got = prize.parse_se_prize(rec)
    assert got is not None
    key, hn, vals = got
    assert key == "2025010405010101"
    assert hn == "7"                                  # ゼロ埋めは正規化
    assert vals["honsho_yen"] == 8_200_000            # 82,000 百円 = 820万円
    assert vals["shutoku_yen"] == 150_000


def test_parse_se_prize_rejects_non_se_and_short():
    assert prize.parse_se_prize(b"RA" + b"0" * 554) is None      # 種別違い
    assert prize.parse_se_prize(b"SE" + b"0" * 10) is None        # 短すぎる
    # キーが数字でない
    bad = bytearray(_fake_se("2025010405010101", 1, 100))
    bad[prize.RACE_KEY_SLICE] = b"XXXXXXXXXXXXXXXX"
    assert prize.parse_se_prize(bytes(bad)) is None


def test_iter_and_build_index(tmp_path):
    f = tmp_path / "SESW_test.jvd"
    blob = (_fake_se("2025010405010101", 1, 82000, confirmed_order=1) + b"\r\n"
            + b"\x00" + _fake_se("2025010405010101", 2, 33000, confirmed_order=2) + b"\r\n"
            + _fake_se("2025010405010102", 5, 0, confirmed_order=8) + b"\r\n")
    f.write_bytes(blob)

    idx = prize.build_prize_index(tmp_path)
    assert set(idx) == {"2025010405010101", "2025010405010102"}
    assert idx["2025010405010101"]["1"]["honsho_yen"] == 8_200_000
    assert idx["2025010405010101"]["2"]["honsho_yen"] == 3_300_000
    assert idx["2025010405010102"]["5"]["honsho_yen"] == 0      # 6着以降は 0

    out = prize.save_prize_index(idx, tmp_path / "cache" / "prize.json")
    assert prize.load_prize_index(out) == idx
    assert prize.load_prize_index(tmp_path / "none.json") == {}


def test_prize_of_lookup():
    idx = {"R1": {"7": {"honsho_yen": 8_200_000, "shutoku_yen": 0}}}
    assert prize.prize_of(idx, "R1", "7") == 8_200_000
    assert prize.prize_of(idx, "R1", "9") is None      # 該当馬なし
    assert prize.prize_of(idx, "ZZ", "7") is None      # 該当レースなし


@pytest.mark.skipif(not config.KEIBA_RAW_RACE_DIR.exists(),
                    reason="生 JV-Data が無い環境ではスキップ")
def test_real_se_prize_follows_jra_top5_rule():
    """実データ検証: 本賞金は1〜5着のみ非ゼロ・単調減少 (JRAは5着まで)。

    このルールが成り立つことがオフセット 366 の正しさの証拠。
    """
    files = sorted(config.KEIBA_RAW_RACE_DIR.glob("SESW*.jvd"))
    if not files:
        pytest.skip("SESW ファイルが無い")
    by_order: dict[int, list[int]] = {}
    for p in files[-4:]:
        for raw in p.read_bytes().split(b"\r\n"):
            rec = raw.lstrip(b"\x00")
            got = prize.parse_se_prize(rec)
            if not got:
                continue
            s = rec[334:336].decode("ascii", "ignore")
            if not s.strip().isdigit():
                continue
            o = int(s)
            y = got[2]["honsho_yen"]
            if o and y is not None:
                by_order.setdefault(o, []).append(y)
    if not all(o in by_order for o in (1, 2, 3, 5, 6)):
        pytest.skip("着順のカバーが不足")

    means = {o: st.mean(v) for o, v in by_order.items()}
    assert means[1] > means[2] > means[3] > means[5] > 0      # 単調減少
    # 6着以降は必ず 0 (JRA のルール)
    for o in (6, 7, 8):
        if o in by_order:
            assert all(v == 0 for v in by_order[o]), f"{o}着に非ゼロの本賞金がある"
    # 1着の賞金が現実的な範囲 (100万〜5億円)
    assert 1_000_000 <= st.median(by_order[1]) <= 500_000_000
