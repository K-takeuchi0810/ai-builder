"""列ラベルと選択肢ラベルの語彙 (labels.py) のテスト。

参加者に見せる文言は labels.py に一元化する。ここが唯一の語彙表であることを
機械的に固定し、API 側やUI側に対応表が複製されるのを防ぐ。
"""

from __future__ import annotations

import os
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
    assert lb.lookback_label(None) == "全レース"
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
