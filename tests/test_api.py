"""configs / predict_service / api のテスト。合成データのみ・CIセーフ。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import api, configs as cf, config as cfgmod, matrix as mx   # noqa: E402
from builder import model, predict_service as svc, specs as sp           # noqa: E402

USER_CFG = {
    "name": "参加者AI 1",
    "step1": ["popularity"],
    "step2": [{"metric": "agg_avg_finish", "match": [], "lookback": 3},
              {"metric": "agg_avg_finish", "match": ["distance"], "lookback": 5}],
}


def _race(race_id="R1", date="20260801", n=10, with_order=True):
    horses = []
    for k in range(1, n + 1):
        horses.append({
            "num": f"{k:02d}", "name": f"馬{k}",
            "order": (k if with_order else 0), "odds": 2.0 + k, "pop": k,
            "n_past_runs": 5,
            "x": {"popularity": float(k),
                  "agg_avg_finish|lb=3|m=": float(k),
                  "agg_avg_finish|lb=5|m=distance": float(k)},
        })
    return {"race_id": race_id, "date": date, "race_name": "テストレース",
            "seg": {}, "trusted": True, "tan": {"01": 300}, "horses": horses,
            "weight_announced": True}


PRESET = {
    "weights": {"popularity": 1.0,
                "agg_avg_finish|lb=3|m=": 2.0,
                "agg_avg_finish|lb=5|m=distance": 2.0},
    "confidence_thresholds": {"solid": 1.0, "strong": 0.3, "n": 100},
}


# ---------------------------------------------------------------------------
# configs: 正規化・ハッシュ・複数選択の平均
# ---------------------------------------------------------------------------
def test_normalize_dedupes_and_sorts():
    raw = {"name": " AI ", "step1": ["popularity", "popularity", "存在しない列"],
           "step2": [{"metric": "agg_avg_finish", "match": ["distance"], "lookback": 3},
                     {"metric": "agg_avg_finish", "match": ["distance"], "lookback": 3},
                     {"metric": "agg_prize", "match": [], "lookback": None}]}
    n = cf.normalize_config(raw)
    assert n["name"] == "AI"
    assert n["step1"] == ["popularity"]                 # 重複と未知キーを除去
    assert len(n["step2"]) == 2                          # 重複セルを除去
    assert [c["metric"] for c in n["step2"]] == ["agg_avg_finish", "agg_prize"]


def test_config_hash_ignores_name_and_order():
    a = {"name": "A", "step1": ["popularity"], "step2": []}
    b = {"name": "B", "step1": ["popularity"], "step2": []}
    c = {"name": "A", "step1": ["popularity", "burden_weight"], "step2": []}
    assert cf.config_hash(a) == cf.config_hash(b)        # 名前は無関係
    assert cf.config_hash(a) != cf.config_hash(c)


def test_multi_cell_selection_averages_base_columns():
    """同じ metric で2セル選択 → 各セルの重みは 1/2 (基底列の単純平均に相当)。"""
    w = cf.column_weights(USER_CFG, PRESET["weights"])
    assert w["popularity"] == 1.0                        # STEP1 はそのまま
    assert w["agg_avg_finish|lb=3|m="] == 1.0            # 2.0 × 1/2
    assert w["agg_avg_finish|lb=5|m=distance"] == 1.0


def test_selected_columns_match_weights():
    cols = cf.selected_columns(USER_CFG)
    w = cf.column_weights(USER_CFG, PRESET["weights"])
    assert {c["id"] for c in cols} == set(w)


def test_save_get_history_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    v1 = cf.save_config(USER_CFG)
    assert v1["version"] == 1
    v2 = cf.save_config({**USER_CFG, "step1": ["popularity", "burden_weight"]},
                        config_id=v1["id"])
    assert v2["version"] == 2                            # マイAI v1 → v2
    hist = cf.config_history(v1["id"])
    assert hist["current_version"] == 2 and len(hist["versions"]) == 2
    assert cf.get_config(v1["id"])["version"] == 2        # 既定は最新
    assert cf.get_config(v1["id"], version=1)["version"] == 1
    assert cf.get_config("nope") is None


# ---------------------------------------------------------------------------
# predict_service: 印・寄与分解・自信度
# ---------------------------------------------------------------------------
def test_predict_assigns_marks_and_contributions():
    got = svc.predict_race(_race(), USER_CFG, PRESET)
    assert [m["mark"] for m in got["marks"][:5]] == ["◎", "○", "▲", "△", "×"]
    assert got["marks"][5]["mark"] == ""                 # 6頭目以降は無印
    # 人気・平均着順とも小さいほど良い → 馬01 が ◎
    assert got["marks"][0]["horse_num"] == "01"
    top = got["marks"][0]
    assert top["contributions"] and all("contribution" in c for c in top["contributions"])
    assert top["coverage"]["n_used"] == 3                # 3列すべて使用
    assert got["config_hash"] == cf.config_hash(USER_CFG)
    assert got["weight_announced"] is True


def test_predict_confidence_labels():
    thresholds = {"solid": 1.0, "strong": 0.3}
    assert svc.ps.confidence_label(2.0, thresholds) == "鉄板級"
    assert svc.ps.confidence_label(0.5, thresholds) == "有力"
    assert svc.ps.confidence_label(0.1, thresholds) == "混戦"
    assert svc.ps.confidence_label(None, thresholds) == "—"
    assert svc.ps.confidence_label(1.0, {}) == "—"
    got = svc.predict_race(_race(), USER_CFG, PRESET)
    assert got["confidence"]["label"] in ("鉄板級", "有力", "混戦")


def test_predict_handles_empty_race():
    got = svc.predict_race({"race_id": "X", "horses": []}, USER_CFG, PRESET)
    assert got["error"] == "no_horses"


# ---------------------------------------------------------------------------
# backtest: 的中率系のみ・人気ベースライン併記・表示期間
# ---------------------------------------------------------------------------
def test_backtest_reports_hit_rates_and_baseline_not_roi():
    m = {"columns": [], "races": [_race(f"R{i}", date="20250801") for i in range(5)]}
    got = svc.backtest(m, USER_CFG, PRESET, date_from="20250701")
    you = got["your_ai"]
    assert you["races"] == 5
    assert you["hit_rate_win"] == 1.0                    # 常に馬01(1着)を◎にする
    assert you["hit_rate_show"] == 1.0
    assert you["hit_rate_in_marks"] == 1.0
    assert you["rank_corr"] == 1.0                       # 印順位と着順が一致
    assert got["baseline_favorite"]["races"] == 5         # 1番人気AIを併記
    # 回収率はメイン指標にしない (設計書 §2)
    assert "roi" not in you and "return" not in json_keys(you)
    assert got["note"].startswith("過去の的中率")


def json_keys(d: dict) -> str:
    return " ".join(d.keys())


def test_backtest_default_period_excludes_training_window():
    """既定の表示期間は学習に使っていない期間 (DISPLAY_BACKTEST_FROM 以降)。"""
    m = {"columns": [], "races": [_race("A", date="20240101"),   # 学習期間内
                                  _race("B", date="20250801")]}  # 表示期間内
    got = svc.backtest(m, USER_CFG, PRESET)
    assert got["period"][0] == cfgmod.DISPLAY_BACKTEST_FROM
    assert got["your_ai"]["races"] == 1                  # 2024 は除外される


def test_backtest_skips_unfinished_races():
    m = {"columns": [], "races": [_race("A", date="20250801", with_order=False)]}
    got = svc.backtest(m, USER_CFG, PRESET, date_from="20250701")
    assert got["your_ai"]["races"] == 0


# ---------------------------------------------------------------------------
# api: ハンドラのロジック (HTTP を立てずに検証)
# ---------------------------------------------------------------------------
def test_feature_catalog_shape():
    cat = api.feature_catalog()
    assert cat["n_base_columns"] == 396
    assert len(cat["step2_metrics"]) == 9
    assert len(cat["step2_matches"]) == 4 and len(cat["step2_lookbacks"]) == 11
    assert cat["step1"] and all("label" in x for x in cat["step1"])
    assert any("獲得本賞金" in n for n in cat["notes"])


def test_handle_predict_found_and_missing():
    api._STATE["daily"] = {"races": [_race("R1")]}
    api._STATE["preset"] = PRESET
    body, status = api.handle_predict({"race_id": "R1", "config": USER_CFG})
    assert status == 200 and body["marks"][0]["mark"] == "◎"
    body, status = api.handle_predict({"race_id": "nope", "config": USER_CFG})
    assert status == 404 and body["error"] == "race_not_found"


def test_handle_backtest_requires_data():
    api._STATE["daily"] = {"races": []}
    api._STATE["preset"] = PRESET
    api._STATE.pop("backtest_matrix", None)
    body, status = api.handle_backtest({"config": USER_CFG})
    assert status == 409 and body["error"] == "no_backtest_data"

    api._STATE["backtest_matrix"] = {"columns": [], "races": [_race("A", date="20250801")]}
    body, status = api.handle_backtest({"config": USER_CFG, "period": {"from": "20250701"}})
    assert status == 200 and body["your_ai"]["races"] == 1
