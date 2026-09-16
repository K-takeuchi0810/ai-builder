"""買い目の組み立て。**実資金の入力に直結するので点数と表記を機械的に固定する。**

このモジュールが出す文字列は、参加者が JRA 公式サイトへ**手で入力する**もの。
1文字違えば違う馬券を買うことになるので、目視ではなく計算で押さえる。
"""

from __future__ import annotations

import os
import sys
from math import comb, perm

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import betslip as bs   # noqa: E402

MARKS = ["◎", "○", "▲", "△", "×"]


def _marks(nums, wakus=None) -> list[dict]:
    """印つきの馬 (印の順に馬番を渡す)。"""
    out = []
    for i, (m, n) in enumerate(zip(MARKS, nums)):
        h = {"mark": m, "horse_num": n}
        if wakus:
            h["waku"] = wakus[i]
        out.append(h)
    return out


def _one(entry, **kw):
    """1件だけ組む。組めなければ理由を上げる。"""
    slip, skipped = bs.build_custom([entry], **kw)
    assert not skipped, skipped
    return slip[0]


def _by(slip) -> dict:
    return {t["key"]: t for t in slip}


# ---------------------------------------------------------------------------
# 既定 (印の並べ替え) — 参加者が何も触らない状態
# ---------------------------------------------------------------------------
def test_default_is_the_axis_flow_from_the_honmei():
    """◎が軸、他の印が相手。触らなければ従来と同じ買い目になること。"""
    sel = bs.default_selection(_marks(["11", "10", "03", "06", "02"]))
    kinds = [(e["type"], e["mode"]) for e in sel]
    assert kinds == [("tan", "each"), ("fuku", "each"),
                     ("umaren", "nagashi"), ("wide", "nagashi"),
                     ("umatan", "nagashi_1st"), ("sanrenpuku", "nagashi")]
    for e in sel[2:]:
        assert e["groups"][0] == ["11"], e            # 軸は◎
        assert e["groups"][1] == ["02", "03", "06", "10"], e   # 相手は馬番順


def test_default_point_counts():
    slip = bs.build(_marks(["11", "10", "03", "06", "02"]))
    n = {t["key"]: t["n"] for t in slip}
    assert n == {"tan": 1, "fuku": 1, "umaren": 4, "wide": 4,
                 "umatan": 4, "sanrenpuku": comb(4, 2)}
    assert bs.total_points(slip) == 20


def test_no_marks_means_no_bets():
    """印が出ないレースでは買い目も出さない。"""
    assert bs.build([]) == []
    assert bs.default_selection([]) == []


def test_a_single_mark_still_produces_the_win_bets():
    """印が1つしか無くても単勝・複勝は作れる (組み合わせ系だけ落ちる)。"""
    sel = bs.default_selection(_marks(["07"]))
    assert [e["type"] for e in sel] == ["tan", "fuku"]
    slip, skipped = bs.build_custom(sel)
    assert skipped == []
    assert bs.total_points(slip) == 2


# ---------------------------------------------------------------------------
# 表記 — 手入力する当人が見る文字列
# ---------------------------------------------------------------------------
def test_ordered_types_keep_their_direction():
    """馬単・三連単は矢印。順序なしの券種と同じ文字列にしない。"""
    umatan = _one({"type": "umatan", "mode": "nagashi_1st",
                   "groups": [["11"], ["10", "03"]]})
    umaren = _one({"type": "umaren", "mode": "nagashi",
                   "groups": [["11"], ["10", "03"]]})
    assert umatan["texts"] == ["11→3", "11→10"]
    assert umaren["texts"] == ["3-11", "10-11"]
    assert umatan["texts"] != umaren["texts"]
    tan3 = _one({"type": "sanrentan", "mode": "formation",
                 "groups": [["11"], ["10"], ["03"]]})
    assert tan3["texts"] == ["11→10→3"]


def test_unordered_types_are_sorted_by_number():
    """順序なしの券種は組の中も番号順。

    公式サイトの入力は番号順のマス目なので、「11-3」のように軸を先に出すと
    転記でずれる。順序ありは並べ替えない (並び自体が着順の指定)。
    """
    got = _one({"type": "sanrenpuku", "mode": "nagashi",
                "groups": [["11"], ["03", "02"]]})
    assert got["texts"] == ["2-3-11"]
    assert got["combos"] == [["02", "03", "11"]]
    ordered = _one({"type": "umatan", "mode": "nagashi_1st",
                    "groups": [["11"], ["02"]]})
    assert ordered["combos"] == [["11", "02"]]     # 軸が先のまま


def test_as_text_uses_the_same_notation():
    slip = bs.build(_marks(["11", "10", "03"]))
    text = bs.as_text(slip)
    assert "馬単 11→10" in text
    assert "馬単 11-10" not in text
    assert "馬連 10-11" in text
    # 1点1行 (公式サイトへ1つずつ入れる形)
    assert len(text.splitlines()) == bs.total_points(slip)


# ---------------------------------------------------------------------------
# 買い方ごとの点数 — 数式と一致すること
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_horses", [2, 3, 5, 8])
def test_box_counts_match_the_formula(n_horses):
    horses = [f"{i:02d}" for i in range(1, n_horses + 1)]
    for key, size, ordered in (("umaren", 2, False), ("umatan", 2, True),
                               ("sanrenpuku", 3, False), ("sanrentan", 3, True)):
        slip, skipped = bs.build_custom(
            [{"type": key, "mode": "box", "groups": [horses]}])
        want = (perm if ordered else comb)(n_horses, size) if n_horses >= size else 0
        if want:
            assert slip[0]["n"] == want, (key, n_horses)
        else:
            assert slip == [] and skipped, (key, n_horses)


def test_umatan_nagashi_modes():
    """馬単の流し3種が、それぞれ違う組を作ること。"""
    groups = [["11"], ["10", "03"]]
    got = {m: _one({"type": "umatan", "mode": m, "groups": groups})["texts"]
           for m in ("nagashi_1st", "nagashi_2nd", "nagashi_both")}
    assert got["nagashi_1st"] == ["11→3", "11→10"]
    assert got["nagashi_2nd"] == ["3→11", "10→11"]
    assert sorted(got["nagashi_both"]) == sorted(got["nagashi_1st"] + got["nagashi_2nd"])


def test_sanrentan_nagashi_fixes_the_slot():
    """三連単の流しは、軸を1着・2着・3着のどこに固定するかで組が変わる。"""
    groups = [["11"], ["10", "03", "06"]]
    got = {m: _one({"type": "sanrentan", "mode": m, "groups": groups})
           for m in ("nagashi_1st", "nagashi_2nd", "nagashi_3rd")}
    for m, t in got.items():
        assert t["n"] == perm(3, 2), m           # 相手3頭の順列
    assert got["nagashi_1st"]["texts"][0].startswith("11→")
    assert got["nagashi_3rd"]["texts"][0].endswith("→11")
    # 2着固定は軸が真ん中
    assert all(x.split("→")[1] == "11" for x in got["nagashi_2nd"]["texts"])


def test_sanrenpuku_two_horse_axis():
    """軸2頭流しは、軸2頭を必ず含んで相手を1頭足す。"""
    got = _one({"type": "sanrenpuku", "mode": "nagashi2",
                "groups": [["11", "10"], ["03", "06"]]})
    assert got["texts"] == ["3-10-11", "6-10-11"]
    assert got["n"] == 2


def test_each_mode_covers_every_selected_horse():
    got = _one({"type": "tan", "mode": "each", "groups": [["11", "10", "03"]]})
    assert got["texts"] == ["3", "10", "11"]


# ---------------------------------------------------------------------------
# フォーメーション
# ---------------------------------------------------------------------------
def test_formation_takes_one_from_each_row():
    """各段から1つずつ。同じ馬が重なる組は成立しないので落とす。"""
    got = _one({"type": "sanrentan", "mode": "formation",
                "groups": [["11"], ["10", "03"], ["06", "02"]]})
    assert got["n"] == 4
    assert got["texts"] == ["11→3→2", "11→3→6", "11→10→2", "11→10→6"]


def test_formation_drops_overlaps_and_duplicates():
    """段に同じ馬が入っていても、重複した組は1点にまとめる。

    順序なしの券種では「2段目の10」と「3段目の10」から同じ組ができる。
    点数が二重に数えられると、手入力の点数と合わなくなる。
    """
    got = _one({"type": "sanrenpuku", "mode": "formation",
                "groups": [["11"], ["10", "03"], ["06", "02", "10"]]})
    assert got["texts"] == ["2-3-11", "2-10-11", "3-6-11", "3-10-11", "6-10-11"]
    assert got["n"] == 5
    assert len(set(map(tuple, got["combos"]))) == got["n"]


def test_formation_for_two_horse_types_has_two_rows():
    """2頭系のフォーメーションは2段 (段数は券種の size が決める)。"""
    t = bs.BY_KEY["umaren"]
    assert len(bs.group_specs(t, "formation")) == 2
    got = _one({"type": "umaren", "mode": "formation",
                "groups": [["11", "10"], ["03"]]})
    assert got["texts"] == ["3-10", "3-11"]


def test_formation_with_no_valid_combination_is_reported():
    """全段が同じ1頭だと組が作れない。**理由を出す。**"""
    slip, skipped = bs.build_custom(
        [{"type": "sanrenpuku", "mode": "formation",
          "groups": [["11"], ["11"], ["11"]]}])
    assert slip == []
    assert "フォーメーション" in skipped[0]["reason"]


# ---------------------------------------------------------------------------
# 枠連 (枠で選ぶ / ゾロ目)
# ---------------------------------------------------------------------------
def test_frames_of_counts_runners_per_frame():
    got = bs.frames_of([{"waku": 1}, {"waku": 1}, {"waku": 2},
                        {"waku": 3}, {"waku": 3}, {"waku": None}])
    assert got == {"1": 2, "2": 1, "3": 2}


def test_wakuren_box_includes_the_same_frame_pair_only_when_it_can_happen():
    """ゾロ目は **その枠に2頭以上いるときだけ** 成立する。"""
    frames = {"1": 2, "2": 1, "3": 2}
    got = _one({"type": "wakuren", "mode": "box", "groups": [["1", "2", "3"]]},
               frames=frames)
    assert got["texts"] == ["1-1", "1-2", "1-3", "2-3", "3-3"]
    assert "2-2" not in got["texts"], "1頭しかいない枠のゾロ目を作ってはいけない"


def test_wakuren_nagashi_includes_the_axis_zoro():
    frames = {"1": 2, "2": 1, "3": 2}
    got = _one({"type": "wakuren", "mode": "nagashi", "groups": [["1"], ["2", "3"]]},
               frames=frames)
    assert got["texts"] == ["1-1", "1-2", "1-3"]
    # 軸枠が1頭ならゾロ目は出ない
    got2 = _one({"type": "wakuren", "mode": "nagashi", "groups": [["2"], ["1", "3"]]},
                frames=frames)
    assert got2["texts"] == ["1-2", "2-3"]


def test_wakuren_rejects_frames_not_in_the_race():
    slip, skipped = bs.build_custom(
        [{"type": "wakuren", "mode": "nagashi", "groups": [["1"], ["8"]]}],
        frames={"1": 2, "2": 1})
    assert slip == []
    assert "枠" in skipped[0]["reason"] and "8" in skipped[0]["reason"]


def test_wakuren_has_no_formation():
    """枠連にフォーメーションは無い (公式にも無く、段の意味が定まらない)。"""
    assert "formation" not in bs.BY_KEY["wakuren"]["modes"]


# ---------------------------------------------------------------------------
# 誤った馬券を作らせないための門
# ---------------------------------------------------------------------------
def test_horses_not_in_the_race_are_rejected():
    slip, skipped = bs.build_custom(
        [{"type": "tan", "mode": "each", "groups": [["11", "99"]]}],
        runners=["11", "10"])
    assert slip == []
    assert "99" in skipped[0]["reason"]


def test_wrong_axis_count_is_reported_with_the_remedy():
    slip, skipped = bs.build_custom(
        [{"type": "sanrenpuku", "mode": "nagashi2",
          "groups": [["11"], ["10", "03"]]}])
    assert slip == []
    assert "2頭" in skipped[0]["reason"], skipped[0]["reason"]


def test_empty_group_is_reported():
    slip, skipped = bs.build_custom(
        [{"type": "umaren", "mode": "nagashi", "groups": [["11"], []]}])
    assert slip == []
    assert "相手" in skipped[0]["reason"], skipped[0]["reason"]


def test_partner_equal_to_the_axis_cannot_be_built():
    """相手が軸だけだと組が作れない。**直し方まで書く。**"""
    slip, skipped = bs.build_custom(
        [{"type": "umaren", "mode": "nagashi", "groups": [["11"], ["11"]]}])
    assert slip == []
    assert "相手を1頭以上" in skipped[0]["reason"], skipped[0]["reason"]


def test_one_bad_entry_does_not_wipe_out_the_others():
    """1件の指定違いで全部消さない。**理由を出して他は作る。**"""
    slip, skipped = bs.build_custom([
        {"type": "umaren", "mode": "nagashi", "groups": [["11"], []]},
        {"type": "tan", "mode": "each", "groups": [["11"]]},
        {"type": "sanrenpuku", "mode": "box", "groups": [["11", "10", "03"]]},
    ])
    assert [t["key"] for t in slip] == ["tan", "sanrenpuku"]
    assert [x["index"] for x in skipped] == [0]      # 位置が分かること


def test_unknown_type_or_mode_is_reported_not_silently_replaced():
    slip, skipped = bs.build_custom([
        {"type": "nazo", "mode": "each", "groups": [["11"]]},
        {"type": "umaren", "mode": "nazo", "groups": [["11"], ["10"]]},
    ])
    assert slip == []
    assert "券種はありません" in skipped[0]["reason"]
    assert "買い方はありません" in skipped[1]["reason"]


def test_wrong_group_count_is_reported():
    slip, skipped = bs.build_custom(
        [{"type": "sanrentan", "mode": "formation", "groups": [["11"], ["10"]]}])
    assert slip == []
    assert "3組" in skipped[0]["reason"], skipped[0]["reason"]


def test_a_broken_payload_does_not_raise():
    """壊れた入力で 500 にしない (API の入力なので何でも来る)。"""
    slip, skipped = bs.build_custom(
        ["nonsense", {"type": "tan", "mode": "each", "groups": [["01"]]}])
    assert [t["key"] for t in slip] == ["tan"]
    assert skipped[0]["index"] == 0
    with pytest.raises(bs.SelectionError):
        bs.build_custom({"horses": []})          # 旧形式 (dict) は明示的に弾く


# ---------------------------------------------------------------------------
# 券種の定義に穴が無いこと
# ---------------------------------------------------------------------------
def test_all_eight_bet_types_are_present():
    keys = [t["key"] for t in bs.BET_TYPES]
    assert keys == ["tan", "fuku", "wakuren", "umaren", "wide",
                    "umatan", "sanrenpuku", "sanrentan"]


def test_every_type_has_modes_and_group_specs():
    for t in bs.BET_TYPES:
        assert t["modes"], t["key"]
        assert t["size"] in (1, 2, 3), t
        assert t["unit"] in ("horse", "frame"), t
        for m in t["modes"]:
            specs = bs.group_specs(t, m)
            assert specs, (t["key"], m)
            for name, need in specs:
                # 見出しが英字のまま出ないこと (段の判定を壊した実績がある)
                lab = bs.group_label(t, m, name)
                assert lab and not lab.isascii(), (t["key"], m, name, lab)


def test_formation_row_count_follows_the_size():
    for t in bs.BET_TYPES:
        if "formation" not in t["modes"]:
            continue
        assert len(bs.group_specs(t, "formation")) == t["size"], t["key"]


# ---------------------------------------------------------------------------
# 金額と QR は扱わない (設計の境界)
# ---------------------------------------------------------------------------
def test_amount_defaults_to_100_yen_and_subtotal_matches_points():
    """金額と QR 生成に踏み込まないこと。

    QR の payload 形式は非公開で、推測して作れば **間違った馬券を実際のお金で
    登録する**。調査記録は docs/SMAPPY_QR_PLAN.md。
    """
    slip = bs.build(_marks(["11", "10", "03"]))
    assert slip
    for t in slip:
        assert t["amount_yen"] == 100
        assert t["amounts_yen"] == [100] * t["n"]
        assert t["subtotal_yen"] == t["n"] * 100
    assert bs.total_yen(slip) == sum(t["n"] * 100 for t in slip)


@pytest.mark.parametrize("bad", [0, 99, 101, 100.5, True, "abc", 1_000_000])
def test_amount_rejects_values_outside_the_100_yen_rules(bad):
    selection = [{"type": "tan", "mode": "each", "groups": [["01"]],
                  "amount_yen": bad}]
    _slip, skipped = bs.build_custom(selection)
    assert skipped and skipped[0]["reason"]


def test_each_expanded_point_can_have_a_different_amount():
    selection = [{"type": "umaren", "mode": "box",
                  "groups": [["01", "02", "03"]],
                  "amounts_yen": [100, 200, 300]}]
    slip, skipped = bs.build_custom(selection)
    assert skipped == []
    assert slip[0]["amount_yen"] is None
    assert slip[0]["amounts_yen"] == [100, 200, 300]
    assert slip[0]["subtotal_yen"] == 600


@pytest.mark.parametrize("amounts", [[100, 200], [100, 200, 301], "100,200,300"])
def test_point_amounts_must_match_the_points_and_100_yen_rules(amounts):
    selection = [{"type": "umaren", "mode": "box",
                  "groups": [["01", "02", "03"]],
                  "amounts_yen": amounts}]
    slip, skipped = bs.build_custom(selection)
    assert slip == []
    assert skipped and skipped[0]["reason"]


def test_betslip_does_not_invent_the_official_qr_payload():
    src = open(bs.__file__, encoding="utf-8").read()
    for bad in ("qrcode.make", "import qrcode", "toDataURL", "segno"):
        assert bad not in src, bad


# ---------------------------------------------------------------------------
# 何を選んだのかが読めること (picks)
# ---------------------------------------------------------------------------
def test_picks_describe_each_input_row():
    """段ごとの選択を、見出しつきで返すこと。

    点の一覧だけだと、フォーメーションで「1着に誰を入れたか」が追えない
    (11→3→2 と 11→10→6 が並んでいても2着候補が読み取れない)。
    """
    got = _one({"type": "sanrentan", "mode": "formation",
                "groups": [["11"], ["10", "03"], ["06", "02", "10"]]})
    assert [(pk["label"], pk["text"]) for pk in got["picks"]] == [
        ("1着候補", "11"), ("2着候補", "3・10"), ("3着候補", "2・6・10")]
    # 番号は int でも取れる (UI が桁合わせを気にしないため)
    assert got["picks"][2]["nums"] == [2, 6, 10]


def test_picks_use_the_axis_and_partner_wording_for_nagashi():
    got = _one({"type": "umaren", "mode": "nagashi",
                "groups": [["11"], ["02", "03"]]})
    assert [(pk["label"], pk["text"]) for pk in got["picks"]] == [
        ("軸", "11"), ("相手", "2・3")]


def test_picks_say_frame_for_wakuren():
    got = _one({"type": "wakuren", "mode": "box", "groups": [["1", "2"]]},
               frames={"1": 2, "2": 1})
    assert got["picks"][0]["label"] == "選ぶ枠"
    assert got["picks"][0]["text"] == "1・2"


def test_picks_separator_does_not_collide_with_the_combination_separator():
    """段の区切りが、組の区切り (`-` / `→`) と混ざらないこと。"""
    assert bs.PICK_SEP not in "-→"
    got = _one({"type": "sanrenpuku", "mode": "nagashi",
                "groups": [["11"], ["02", "03", "06"]]})
    joined = " ".join(pk["text"] for pk in got["picks"])
    assert "-" not in joined and "→" not in joined, joined


def test_every_mode_returns_one_pick_row_per_input_row():
    """全券種・全買い方で、picks の数が入力欄の数と一致すること。"""
    horses = [f"{i:02d}" for i in range(1, 7)]
    frames = {str(i): 2 for i in range(1, 5)}
    for t in bs.BET_TYPES:
        pool = list(frames) if t["unit"] == "frame" else horses
        for mode in t["modes"]:
            specs = bs.group_specs(t, mode)
            groups = []
            for _name, need in specs:
                groups.append(pool[:need] if need else pool[-3:])
            slip, skipped = bs.build_custom(
                [{"type": t["key"], "mode": mode, "groups": groups}],
                frames=frames if t["unit"] == "frame" else None)
            assert not skipped, (t["key"], mode, skipped)
            assert len(slip[0]["picks"]) == len(specs), (t["key"], mode)
