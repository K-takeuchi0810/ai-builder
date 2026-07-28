"""買い目の組み立て。**実資金の入力に直結するので点数と表記を機械的に固定する。**

このモジュールが出す文字列は、参加者が JRA 公式サイトへ**手で入力する**もの。
1文字違えば違う馬券を買うことになるので、目視ではなく計算で押さえる。
"""

from __future__ import annotations

import os
import sys
from itertools import combinations, permutations
from math import comb

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import betslip as bs   # noqa: E402

MARKS = ["◎", "○", "▲", "△", "×"]


def _marks(nums) -> list[dict]:
    """印つきの馬 (印の順に馬番を渡す)。"""
    return [{"mark": m, "horse_num": n} for m, n in zip(MARKS, nums)]


def _by(slip) -> dict:
    return {t["key"]: t for t in slip}


# ---------------------------------------------------------------------------
# 既定 (印の並べ替え) — 参加者が何も触らない状態
# ---------------------------------------------------------------------------
def test_default_is_the_axis_flow_from_the_honmei():
    """◎が軸、他の印が相手。触らなければ従来と同じ買い目になること。"""
    sel = bs.default_selection(_marks(["11", "10", "03", "06", "02"]))
    assert sel["axis"] == ["11"], sel["axis"]
    assert sel["horses"] == ["02", "03", "06", "10", "11"]   # 馬番順
    assert sel["modes"]["umaren"] == "nagashi"
    assert sel["modes"]["umatan"] == "nagashi_1st"


def test_default_point_counts():
    slip = bs.build(_marks(["11", "10", "03", "06", "02"]))
    n = {t["key"]: t["n"] for t in slip}
    assert n == {"tan": 1, "fuku": 1, "umaren": 4, "wide": 4,
                 "umatan": 4, "sanrenpuku": comb(4, 2)}
    assert bs.total_points(slip) == 20


def test_no_marks_means_no_bets():
    """印が出ないレースでは買い目も出さない。"""
    assert bs.build([]) == []
    assert bs.default_selection([]) == {"horses": [], "axis": [], "modes": {}}


# ---------------------------------------------------------------------------
# 表記 — 手入力する当人が見る文字列
# ---------------------------------------------------------------------------
def test_ordered_types_keep_their_direction():
    """馬単は矢印。馬連と同じ文字列にしない (券種の違いを誤学習させない)。"""
    slip = _by(bs.build(_marks(["11", "10", "03"])))
    assert slip["umatan"]["texts"] == ["11→3", "11→10"]
    assert slip["umaren"]["texts"] == ["3-11", "10-11"]
    assert slip["umaren"]["texts"] != slip["umatan"]["texts"]


def test_unordered_types_are_sorted_by_horse_number():
    """順序なしの券種は組の中も馬番順。

    公式サイトの入力は馬番順のマス目なので、「11-3」のように軸を先に出すと
    転記でずれる。馬単は並べ替えない (並び自体が着順の指定)。
    """
    slip = _by(bs.build(_marks(["11", "03", "02"])))
    assert slip["umaren"]["texts"] == ["2-11", "3-11"]
    assert slip["sanrenpuku"]["texts"] == ["2-3-11"]
    # combos も表記と同じ並び (読み合わせで目が滑らないように)
    assert slip["umaren"]["combos"] == [["02", "11"], ["03", "11"]]
    # 馬単は軸が先のまま
    assert slip["umatan"]["combos"] == [["11", "02"], ["11", "03"]]


def test_as_text_uses_the_same_notation():
    slip = bs.build(_marks(["11", "10", "03"]))
    text = bs.as_text(slip)
    assert "馬単 11→10" in text
    assert "馬単 11-10" not in text
    assert "馬連 10-11" in text
    # 1点1行 (公式サイトへ1つずつ入れる形)
    assert len(text.splitlines()) == bs.total_points(slip)


# ---------------------------------------------------------------------------
# 組み方ごとの点数 — 数式と一致すること
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_horses", [2, 3, 5, 8])
def test_box_counts_match_the_formula(n_horses):
    horses = [f"{i:02d}" for i in range(1, n_horses + 1)]
    sel = {"horses": horses, "axis": horses[:1],
           "modes": {"umaren": "box", "umatan": "box", "sanrenpuku": "box"}}
    slip, _ = bs.build_custom(sel)
    got = {t["key"]: t["n"] for t in slip}
    assert got["umaren"] == comb(n_horses, 2)
    assert got["umatan"] == n_horses * (n_horses - 1)      # 順列
    if n_horses >= 3:
        assert got["sanrenpuku"] == comb(n_horses, 3)
    else:
        assert "sanrenpuku" not in got


def test_umatan_modes():
    """馬単の4つの組み方が、それぞれ違う組を作ること。"""
    sel = {"horses": ["11", "10", "03"], "axis": ["11"], "modes": {}}
    got = {}
    for mode in ("nagashi_1st", "nagashi_2nd", "nagashi_both", "box"):
        slip, _ = bs.build_custom({**sel, "modes": {"umatan": mode}})
        got[mode] = slip[0]["texts"]
    assert got["nagashi_1st"] == ["11→3", "11→10"]
    assert got["nagashi_2nd"] == ["3→11", "10→11"]
    assert got["nagashi_both"] == ["11→3", "11→10", "3→11", "10→11"]
    assert len(got["box"]) == 6
    # 1着固定と2着固定は互いに逆
    assert got["nagashi_1st"] != got["nagashi_2nd"]


def test_sanrenpuku_two_horse_axis():
    """軸2頭流しは、軸2頭を必ず含んで相手を1頭足す。"""
    slip, skipped = bs.build_custom(
        {"horses": ["11", "10", "03", "06"], "axis": ["11", "10"],
         "modes": {"sanrenpuku": "nagashi2"}})
    assert skipped == []
    assert slip[0]["texts"] == ["3-10-11", "6-10-11"]
    assert slip[0]["n"] == 2


def test_each_mode_covers_every_selected_horse():
    slip, _ = bs.build_custom({"horses": ["11", "10", "03"], "axis": ["11"],
                               "modes": {"tan": "each", "fuku": "jiku"}})
    got = _by(slip)
    assert got["tan"]["texts"] == ["3", "10", "11"]
    assert got["fuku"]["texts"] == ["11"]        # 軸だけ


# ---------------------------------------------------------------------------
# 誤った馬券を作らせないための門
# ---------------------------------------------------------------------------
def test_horses_not_in_the_race_are_rejected():
    """出走していない馬番は組まない。**これは全体の誤りなので例外。**"""
    with pytest.raises(bs.SelectionError) as e:
        bs.build_custom({"horses": ["11", "99"], "axis": ["11"],
                         "modes": {"tan": "jiku"}}, runners=["11", "10"])
    assert "99" in str(e.value)


def test_axis_must_be_among_the_selected_horses():
    with pytest.raises(bs.SelectionError):
        bs.build_custom({"horses": ["11", "10"], "axis": ["03"],
                         "modes": {"tan": "jiku"}})


def test_too_many_axis_horses_is_rejected():
    with pytest.raises(bs.SelectionError):
        bs.build_custom({"horses": ["11", "10", "03"], "axis": ["11", "10", "03"],
                         "modes": {"tan": "jiku"}})


def test_one_bad_type_does_not_wipe_out_the_others():
    """1券種の指定違いで全部消さない。**理由を出して他は作る。**

    全体をエラーにすると、何が起きたのか分からないまま買い目が消える。
    """
    slip, skipped = bs.build_custom(
        {"horses": ["11", "10", "03", "06"], "axis": ["11", "10"],
         "modes": {"umaren": "nagashi", "sanrenpuku": "nagashi2", "wide": "box"}})
    made = {t["key"] for t in slip}
    assert made == {"sanrenpuku", "wide"}
    assert [x["key"] for x in skipped] == ["umaren"]
    # 直し方まで書く (理由だけだと手が止まる)
    assert "軸を1頭にするか" in skipped[0]["reason"], skipped[0]["reason"]


def test_unknown_mode_is_reported_not_silently_replaced():
    slip, skipped = bs.build_custom({"horses": ["11", "10"], "axis": ["11"],
                                     "modes": {"umaren": "nazo"}})
    assert slip == []
    assert skipped[0]["key"] == "umaren"
    assert "nazo" in skipped[0]["reason"]


def test_type_with_too_few_horses_is_reported():
    slip, skipped = bs.build_custom({"horses": ["11", "10"], "axis": ["11"],
                                     "modes": {"sanrenpuku": "box"}})
    assert slip == []
    assert skipped[0]["key"] == "sanrenpuku"
    assert "3頭以上" in skipped[0]["reason"]


def test_types_switched_off_produce_nothing():
    slip, skipped = bs.build_custom({"horses": ["11", "10"], "axis": ["11"],
                                     "modes": {"umaren": None, "wide": "nagashi"}})
    assert [t["key"] for t in slip] == ["wide"]
    assert skipped == []          # OFF は「組めなかった」ではない


# ---------------------------------------------------------------------------
# 金額と QR は扱わない (設計の境界)
# ---------------------------------------------------------------------------
def test_no_amount_and_no_qr_anywhere():
    """金額と QR 生成に踏み込まないこと。

    QR の payload 形式は非公開で、推測して作れば **間違った馬券を実際のお金で
    登録する**。調査記録は docs/SMAPPY_QR_PLAN.md。
    """
    slip = bs.build(_marks(["11", "10", "03"]))
    for t in slip:
        assert "amount" not in t and "yen" not in t and "kingaku" not in t
        assert "qr" not in " ".join(t.keys()).lower()
    src = open(bs.__file__, encoding="utf-8").read()
    # 生成ライブラリを呼んでいないこと (コメントでの言及は許す)
    for bad in ("qrcode.make", "import qrcode", "toDataURL", "segno"):
        assert bad not in src, bad


def test_every_type_has_at_least_one_mode_and_a_default():
    """券種の定義に穴が無いこと (既定は先頭のモード)。"""
    for t in bs.BET_TYPES:
        assert t["modes"], t["key"]
        assert t["size"] in (1, 2, 3), t
        for m in t["modes"]:
            assert m in bs.AXIS_SIZE, (t["key"], m)
