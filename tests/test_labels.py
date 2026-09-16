"""列ラベルと選択肢ラベルの語彙 (labels.py) のテスト。

参加者に見せる文言は labels.py に一元化する。ここが唯一の語彙表であることを
機械的に固定し、API 側やUI側に対応表が複製されるのを防ぐ。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import api, labels as lb, matrix, model, specs as sp   # noqa: E402


def test_aggregate_labels_include_the_cell():
    """集計列のラベルは **どのセルか** (一致条件・さかのぼる範囲) まで含めること。

    設計書 §3.1 の「タイム指数(同距離・直近3走) +2.1」という粒度の説明は、
    セルを含んだ列名がないと成立しない。
    """
    assert lb.column_label("agg_time_index", ["distance"], 3) == "タイム指数(同距離・直近3走)"
    assert lb.column_label("agg_avg_finish", ["track"], 5) == "平均着順(同競馬場・直近5走)"
    assert lb.column_label("agg_prize", [], None) == "獲得本賞金(全レース・全走)"
    assert lb.column_label("agg_corner_first", ["surface"], 1) \
        == "第1コーナー通過順位(芝ダート別・直近1走)"


def test_non_aggregate_labels_have_no_cell_suffix():
    assert lb.column_label("popularity") == "人気(市場)"
    assert "(" not in lb.column_label("weight_carried")


def test_unknown_key_passes_through():
    """未知のキーは捏造せずそのまま返す。"""
    assert lb.column_label("no_such_feature") == "no_such_feature"


def test_prize_label_says_honshokin():
    """賞金は「獲得本賞金」。付加賞・褒賞金を含まないので「賞金総額」と書かない。"""
    label = lb.column_label("agg_prize", [], 3)
    assert "本賞金" in label
    assert "総額" not in label


def test_every_maib_column_gets_a_distinct_label():
    """425列すべてが一意なラベルを持つこと。

    同じラベルの列が2つあると、寄与分解の行が区別できず参加者が誤読する。
    """
    cols = matrix._columns(sp.maib_all_specs())
    labels = [c["label"] for c in cols]
    dupes = {x for x in labels if labels.count(x) > 1}
    assert not dupes, f"ラベル重複: {sorted(dupes)[:5]}"
    assert len(labels) == 425


def test_no_developer_jargon_in_labels():
    """参加者に出るラベルに開発用語を残さない。"""
    cols = matrix._columns(sp.maib_all_specs())
    for c in cols:
        for banned in ("可変集計", "lb=", "m=", "agg_", "None"):
            assert banned not in c["label"], (c["id"], c["label"])


def test_match_and_lookback_labels_have_long_and_short_forms():
    assert lb.match_label([]) == "全レース"
    assert lb.match_label([], short=True) == "全レース"
    assert lb.match_label(["distance"]) == "距離が同じ"
    assert lb.match_label(["distance"], short=True) == "同距離"
    # C-2: 一致条件の「全レース」と衝突しない長い形
    assert lb.lookback_label(None) == "これまでの全走"
    assert lb.lookback_label(None, short=True) == "全走"
    assert lb.lookback_label(3) == "直近3レース"
    assert lb.lookback_label(3, short=True) == "直近3走"


def test_match_order_does_not_change_the_label():
    """一致条件の指定順でラベルが変わらないこと (正規化して引く)。"""
    assert lb.match_label(["distance"]) == lb.match_label(("distance",))


def test_api_catalog_uses_the_same_vocabulary():
    """API の選択肢ラベルが labels.py 由来であること (2箇所に書かない)。"""
    cat = api.feature_catalog()
    assert [m["label"] for m in cat["step2_matches"]] == \
        [lb.match_label(m) for m in sp.MAIB_MATCHES]
    assert [m["label"] for m in cat["step2_lookbacks"]] == \
        [lb.lookback_label(x) for x in sp.MAIB_LOOKBACKS]
    # STEP2 の集計対象名にも開発用語を残さない
    for m in cat["step2_metrics"]:
        assert "可変集計" not in m["label"]
    for s in cat["step1"]:
        assert "可変集計" not in s["label"]


def test_column_label_matches_what_matrix_stores():
    """matrix._columns が付けるラベルと labels.column_label が一致すること。"""
    for spec in sp.maib_all_specs()[:40]:
        col = matrix._columns([spec])[0]
        assert col["label"] == lb.column_label(
            spec["key"], spec.get("match"), spec.get("lookback"))


def test_label_change_does_not_invalidate_matrix_cache():
    """列ハッシュは id のみに依存すること (ラベル変更で 4.6GB の再構築が起きない)。"""
    cols = matrix._columns(sp.maib_all_specs())
    h1 = matrix._col_hash(cols)
    renamed = [dict(c, label="まったく違う名前") for c in cols]
    assert matrix._col_hash(renamed) == h1


# ---------------------------------------------------------------------------
# 初心者対応: 自然語ラベル・グループ・用語辞書 (labels.py が唯一の正本)
# ---------------------------------------------------------------------------
def test_step1_labels_have_no_developer_notation():
    """受け入れ条件 (機械検査): STEP1 のラベルに「×」記法・開発者語彙が無い。"""
    for s in sp.maib_participant_step1_specs():
        label = lb.step1_label(s["key"])
        # 「父×芝ダート」のような記法と、英字キー・アンダースコアの混入を禁止
        assert "×" not in label, (s["key"], label)
        assert "_" not in label, (s["key"], label)
        assert not re.search(r"[A-Za-z]{3,}", label), (s["key"], label)
        # キーがそのまま出ていない (訳し忘れの検出)
        assert label != s["key"], s["key"]
    # 「30d」「4角」のような略記も残っていないこと
    labels = [lb.step1_label(s["key"]) for s in sp.maib_participant_step1_specs()]
    for bad in ("30d", "90d", "4角", "top3"):
        assert not any(bad in x for x in labels), bad


def test_every_step1_key_has_a_natural_label_and_group():
    """28項目すべてに自然語ラベルとグループが定義されていること。"""
    groups = {g[0] for g in lb.STEP1_GROUPS}
    for s in sp.maib_step1_specs():
        key = s["key"]
        assert key in lb._STEP1, f"{key} の対訳が未定義"
        assert lb.step1_group(key) in groups, (key, lb.step1_group(key))


def test_step1_groups_are_unique_and_described():
    keys = [g[0] for g in lb.STEP1_GROUPS]
    assert len(keys) == len(set(keys))
    for _k, label, desc in lb.STEP1_GROUPS:
        assert label and desc


def test_glossary_entries_are_beginner_readable():
    """用語辞書に禁止語彙が混ざらないこと・説明が空でないこと。"""
    for e in lb.glossary():
        assert e["term"] and e["desc"], e
        for banned in ("儲か", "買い目", "回収率", "ROI", "必勝", "稼げ"):
            assert banned not in e["desc"], (e["key"], banned)
        assert len(e["desc"]) >= 10, e["key"]        # 一言で済ませていない


def test_glossary_covers_the_terms_the_ui_references():
    """UI が参照する用語キーがすべて辞書にあること (リンク切れ防止)。"""
    need = {"mark", "honmei", "taikou", "tanana", "renka", "chuui", "mujirushi",
            "win_odds", "popularity", "confidence", "coverage", "backtest",
            "low_sample"}
    assert need <= set(lb.GLOSSARY)


def test_step1_glossary_links_resolve():
    """項目に紐づけた用語キーが辞書に存在すること。"""
    for s in sp.maib_step1_specs():
        k = lb.glossary_key(s["key"])
        if k is not None:
            assert k in lb.GLOSSARY, (s["key"], k)


def test_mark_legend_order_matches_the_marks():
    """凡例が ◎○▲△× + 無印 の順で、印の並びと一致すること。"""
    from builder import predict_service as svc
    cat = api.feature_catalog()
    legend = cat["mark_legend"]
    assert [m["mark"] for m in legend[:5]] == svc.MARKS
    assert legend[5]["mark"] == ""                   # 無印
    assert "本命" in legend[0]["term"]


def test_low_sample_is_marked_before_selection(monkeypatch):
    """受け入れ条件: 低サンプル項目が選択前にマークされている。

    feature_catalog は `api._STATE["preset"]` を見る (--weights を尊重するため)。
    他のテストが合成プリセットを入れたまま残すと結果が変わるので、
    このテストは **本番の重みを明示的に置いて** から検証する。
    """
    from builder import presets as ps
    monkeypatch.setitem(api._STATE, "preset", ps.load_presets())
    cat = api.feature_catalog()
    thin = [m for m in cat["step2_metrics"] if m.get("low_sample")]
    names = [m["label"] for m in thin]
    assert any("コーナー" in n for n in names), names
    assert any("賞金" in n for n in names), names
    for m in thin:
        assert isinstance(m["min_train_races"], int) and m["min_train_races"] >= 0


def test_starter_preset_is_offered(monkeypatch):
    """初心者の空白画面問題への最小の答えが用意されていること。"""
    from builder import presets as ps
    monkeypatch.setitem(api._STATE, "preset", ps.load_presets())
    sp_preset = api.feature_catalog()["starter_preset"]
    assert sp_preset["step1"] and len(sp_preset["step1"]) == 3
    assert "popularity" not in sp_preset["step1"]     # 判断A
    assert sp_preset["label"] and sp_preset["desc"]
    presets = api.feature_catalog()["starter_presets"]
    assert {p["key"] for p in presets} == {"standard", "debut", "maiden"}
    debut = next(p for p in presets if p["key"] == "debut")
    assert "recent_avg_finish" not in debut["step1"]
    assert all(k not in sp.PARTICIPANT_UNAVAILABLE_KEYS for k in debut["step1"])
