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
    "step1": ["burden_weight"],
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
            "x": {"popularity": float(k),      # 判断A で参加者経路からは落ちる
                  "burden_weight": float(k),
                  "agg_avg_finish|lb=3|m=": float(k),
                  "agg_avg_finish|lb=5|m=distance": float(k)},
        })
    return {"race_id": race_id, "date": date, "race_name": "テストレース",
            "seg": {}, "trusted": True, "tan": {"01": 300}, "horses": horses,
            "weight_announced": True}


PRESET = {
    "weights": {"burden_weight": 1.0,
                "agg_avg_finish|lb=3|m=": 2.0,
                "agg_avg_finish|lb=5|m=distance": 2.0},
    # scale キーが無いと confidence_label は解釈を拒否する (古い絶対閾値との混同防止)
    "confidence_thresholds": {"solid": 1.0, "strong": 0.3, "n": 100,
                              "scale": "normalized_gap_v1"},
}


# ---------------------------------------------------------------------------
# configs: 正規化・ハッシュ・複数選択の平均
# ---------------------------------------------------------------------------
def test_normalize_dedupes_and_sorts():
    raw = {"name": " AI ", "step1": ["burden_weight", "burden_weight", "存在しない列"],
           "step2": [{"metric": "agg_avg_finish", "match": ["distance"], "lookback": 3},
                     {"metric": "agg_avg_finish", "match": ["distance"], "lookback": 3},
                     {"metric": "agg_prize", "match": [], "lookback": None}]}
    n = cf.normalize_config(raw)
    assert n["name"] == "AI"
    assert n["step1"] == ["burden_weight"]              # 重複と未知キーを除去
    assert len(n["step2"]) == 2                          # 重複セルを除去
    assert [c["metric"] for c in n["step2"]] == ["agg_avg_finish", "agg_prize"]


def test_config_hash_ignores_name_and_order():
    a = {"name": "A", "step1": ["burden_weight"], "step2": []}
    b = {"name": "B", "step1": ["burden_weight"], "step2": []}
    c = {"name": "A", "step1": ["burden_weight", "draw_position"], "step2": []}
    assert cf.config_hash(a) == cf.config_hash(b)        # 名前は無関係
    assert cf.config_hash(a) != cf.config_hash(c)


def test_multi_cell_selection_averages_base_columns():
    """同じ metric で2セル選択 → 各セルの重みは 1/2 (基底列の単純平均に相当)。"""
    w = cf.column_weights(USER_CFG, PRESET["weights"])
    assert w["burden_weight"] == 1.0                     # STEP1 はそのまま
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
    v2 = cf.save_config({**USER_CFG, "step1": ["burden_weight", "draw_position"]},
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
    thresholds = {"solid": 1.0, "strong": 0.3, "scale": svc.ps.CONFIDENCE_SCALE}
    assert svc.ps.confidence_label(2.0, thresholds) == "鉄板級"
    assert svc.ps.confidence_label(0.5, thresholds) == "有力"
    assert svc.ps.confidence_label(0.1, thresholds) == "混戦"
    assert svc.ps.confidence_label(None, thresholds) == "—"
    assert svc.ps.confidence_label(1.0, {}) == "—"
    got = svc.predict_race(_race(), USER_CFG, PRESET)
    assert got["confidence"]["label"] in ("鉄板級", "有力", "混戦")


def test_confidence_label_refuses_old_absolute_thresholds():
    """生のスコア差で作られた古い閾値は解釈せず「—」にすること。

    尺度の違う値を突き合わせるとラベルが「混戦」に張り付き、参加者に嘘の
    自信度を見せる (実測: 人気を選ばない設定で 100% 混戦)。黙って使わない。
    """
    old = {"solid": 0.4963, "strong": 0.2767, "n": 15549}       # scale キーが無い
    assert svc.ps.confidence_label(2.0, old) == "—"
    assert svc.ps.confidence_label(0.1, old) == "—"


def test_confidence_gap_is_scale_invariant():
    """全重みを定数倍しても自信度の判定値が変わらないこと。

    これが成り立つから、425列で決めた閾値を数項目の設定にも当てられる。
    """
    race = _race()
    cfg = {"step1": ["burden_weight"],
           "step2": [{"metric": "agg_avg_finish", "match": [], "lookback": 3}]}
    a = svc.predict_race(race, cfg, PRESET)["confidence"]["normalized_gap"]
    scaled = dict(PRESET, weights={k: v * 100 for k, v in PRESET["weights"].items()})
    b = svc.predict_race(race, cfg, scaled)["confidence"]["normalized_gap"]
    assert a == pytest.approx(b, rel=1e-6)
    # 生の差は 100 倍になる (= 生の差では閾値を共有できない)
    raw_a = svc.predict_race(race, cfg, PRESET)["confidence"]["score_gap"]
    raw_b = svc.predict_race(race, cfg, scaled)["confidence"]["score_gap"]
    assert raw_b == pytest.approx(raw_a * 100, rel=1e-3)   # 応答は4桁丸め


def test_normalized_gap_edge_cases():
    assert svc.ps.normalized_gap([]) is None
    assert svc.ps.normalized_gap([1.0]) is None
    assert svc.ps.normalized_gap([2.0, 2.0, 2.0]) == 0.0       # 全馬同点
    # 順序に依存しない (内部でソートする)
    assert svc.ps.normalized_gap([1.0, 5.0, 2.0]) == \
        svc.ps.normalized_gap([5.0, 2.0, 1.0])


def test_predict_warns_when_presets_missing():
    """プリセット重みが選択列をカバーしないと印は無意味 → 黙らず警告する。"""
    got = svc.predict_race(_race(), USER_CFG, {"weights": {}})
    codes = {w["code"] for w in got["warnings"]}
    assert "no_preset_weights" in codes
    assert got["n_columns_used"] == 0
    # 重みが揃っていれば警告なし
    ok = svc.predict_race(_race(), USER_CFG, PRESET)
    assert ok["warnings"] == [] and ok["n_columns_used"] == 3


def test_predict_warns_when_all_columns_gated_out():
    """全列がカバレッジ不足で使えない場合も警告する。"""
    race = _race(n=12)
    for i, h in enumerate(race["horses"]):        # 2頭だけ値を残す = ゲート落ち
        if i >= 2:
            h["x"] = {k: None for k in h["x"]}
    got = svc.predict_race(race, USER_CFG, PRESET)
    codes = {w["code"] for w in got["warnings"]}
    assert "all_columns_gated_out" in codes
    assert got["n_columns_used"] == 0


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


def test_backtest_warns_when_no_races_in_period():
    """0レースで的中率 None を返すと「成績が悪い」と誤読されるので警告する。"""
    m = {"columns": [], "races": [_race("A", date="20240101")]}
    got = svc.backtest(m, USER_CFG, PRESET, date_from="20250701")
    assert got["your_ai"]["races"] == 0
    assert {w["code"] for w in got["warnings"]} == {"no_races_in_period"}
    # レースがあれば警告なし
    ok = svc.backtest({"columns": [], "races": [_race("B", date="20250801")]},
                      USER_CFG, PRESET, date_from="20250701")
    assert ok["warnings"] == []


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


def test_static_serving_resolves_and_blocks_traversal(tmp_path, monkeypatch):
    """web/ 配下のみ配信し、web/ の外を指すパスは拒否すること。"""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (web / "app.css").write_text(":root{}", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve", encoding="utf-8")
    monkeypatch.setattr(api, "WEB_DIR", web)

    sent = {}

    class FakeHandler:
        def __init__(self):
            self.wfile = self
            self.headers = {}

        def send_response(self, code):
            sent["status"] = code

        def send_header(self, k, v):
            self.headers[k] = v

        def end_headers(self):
            pass

        def write(self, body):
            sent["body"] = body

    h = FakeHandler()
    api._serve_static(h, "index.html")
    assert sent["status"] == 200
    assert h.headers["Content-Type"].startswith("text/html")
    assert b"ok" in sent["body"]

    h2 = FakeHandler()
    api._serve_static(h2, "app.css")
    assert h2.headers["Content-Type"].startswith("text/css")

    # ディレクトリ指定は index.html にフォールバック
    h3 = FakeHandler()
    api._serve_static(h3, "")
    assert sent["status"] == 200

    # パストラバーサルは 403
    h4 = FakeHandler()
    api._serve_static(h4, "../secret.txt")
    assert sent["status"] == 403

    # 無いファイルは 404
    h5 = FakeHandler()
    api._serve_static(h5, "nope.js")
    assert sent["status"] == 404


def test_handle_leaderboard_requires_daily_matrix():
    api._STATE["daily"] = {"races": []}
    api._STATE["preset"] = PRESET
    body, status = api.handle_leaderboard()
    assert status == 409 and body["error"] == "daily_matrix_not_built"


def test_handle_backtest_requires_data():
    api._STATE["daily"] = {"races": []}
    api._STATE["preset"] = PRESET
    api._STATE.pop("backtest_matrix", None)
    body, status = api.handle_backtest({"config": USER_CFG})
    assert status == 409 and body["error"] == "no_backtest_data"

    api._STATE["backtest_matrix"] = {"columns": [], "races": [_race("A", date="20250801")]}
    body, status = api.handle_backtest({"config": USER_CFG, "period": {"from": "20250701"}})
    assert status == 200 and body["your_ai"]["races"] == 1


# ---------------------------------------------------------------------------
# 学習サンプルが薄い項目の開示 (コーナー・賞金は生データの保存範囲が短い)
# ---------------------------------------------------------------------------
_THIN_PRESET = dict(PRESET, low_sample_columns=[
    {"column": "agg_avg_finish|lb=5|m=distance", "races_passed_gate": 173, "threshold": 500},
    {"column": "not_selected_column", "races_passed_gate": 12, "threshold": 500},
])


def test_predict_warns_when_a_selected_column_has_thin_training_data():
    """薄い学習サンプルの項目を選んだら、項目名と学習レース数を明示すること。

    重み 173 レース学習の項目と 15,000 レース学習の項目を同じ見た目で出すと、
    参加者が確度を誤解する (実資金の判断に使われる)。
    """
    cfg = {"step1": ["burden_weight"],
           "step2": [{"metric": "agg_avg_finish", "match": ["distance"], "lookback": 5}]}
    got = svc.predict_race(_race(), cfg, _THIN_PRESET)
    warn = next(w for w in got["warnings"] if w["code"] == "low_sample_columns")
    assert "173" in warn["message"]
    assert warn["n_columns"] == 1
    assert warn["columns"][0]["train_races"] == 173
    # 選んでいない列は警告に混ぜない
    assert all("not_selected" not in c["label"] for c in warn["columns"])
    # 印は出し続ける (ブロックしない)
    assert got["marks"] and got["marks"][0]["mark"] == "◎"


def test_predict_does_not_warn_when_no_selected_column_is_thin():
    cfg = {"step1": ["burden_weight"], "step2": []}
    got = svc.predict_race(_race(), cfg, _THIN_PRESET)
    assert not any(w["code"] == "low_sample_columns" for w in got["warnings"])


def test_predict_flags_thin_columns_in_contributions():
    """寄与の各行にも印を付ける (「なぜ◎か」を開いた参加者がそこで判断する)。"""
    cfg = {"step1": ["burden_weight"],
           "step2": [{"metric": "agg_avg_finish", "match": ["distance"], "lookback": 5}]}
    got = svc.predict_race(_race(), cfg, _THIN_PRESET)
    contribs = {c["id"]: c for c in got["marks"][0]["contributions"]}
    thin = contribs["agg_avg_finish|lb=5|m=distance"]
    assert thin["low_sample"] is True and thin["train_races"] == 173
    assert "low_sample" not in contribs["burden_weight"]   # 厚い列には付けない


def test_predict_low_sample_warning_absent_when_preset_has_no_report():
    """low_sample_columns を持たない古いプリセットでも落ちないこと。"""
    cfg = {"step1": ["burden_weight"], "step2": []}
    got = svc.predict_race(_race(), cfg, PRESET)
    assert not any(w["code"] == "low_sample_columns" for w in got["warnings"])


def test_predict_carries_header_fields_for_the_ui():
    """予想画面が一覧レスポンスに依存しないよう、ヘッダ情報を同梱すること。"""
    race = dict(_race(), start_time="15:45", race_num="11",
                odds_as_of="2026-08-01T15:31:07", trusted=False)
    got = svc.predict_race(race, {"step1": ["burden_weight"], "step2": []}, PRESET)
    assert got["start_time"] == "15:45" and got["race_num"] == "11"
    assert got["odds_as_of"] == "2026-08-01T15:31:07"
    assert got["odds_trusted"] is False
    assert got["n_columns_selected"] == 1


# ---------------------------------------------------------------------------
# 判断A: 「人気(市場)」を参加者AIから除外する (設計書 v0.3 §2)
# ---------------------------------------------------------------------------
_CFG_WITH_POP = {"name": "旧設定", "step1": ["popularity", "burden_weight"],
                 "step2": [{"metric": "agg_avg_finish", "match": [], "lookback": 3}]}


def test_popularity_is_not_offered_as_a_choice():
    """選択肢に「人気(市場)」が出ないこと。"""
    cat = api.feature_catalog()
    assert "popularity" not in [s["key"] for s in cat["step1"]]
    assert all("人気" not in s["label"] for s in cat["step1"])
    # STEP2 の集計対象にも人気系は無い
    assert all("popularity" not in m["metric"] for m in cat["step2_metrics"])


def test_popularity_is_dropped_from_a_saved_config():
    """v0.3 以前に保存された設定から人気が落ちること (マイグレーション)。"""
    n = cf.normalize_config(_CFG_WITH_POP)
    assert "popularity" not in n["step1"]
    assert n["step1"] == ["burden_weight"]              # 他の項目は残る
    assert cf.excluded_in_config(_CFG_WITH_POP) == ["popularity"]
    assert cf.excluded_in_config({"step1": ["burden_weight"], "step2": []}) == []


def test_popularity_never_reaches_columns_or_weights():
    """選択列・重みのどちらにも人気が現れないこと。"""
    cols = cf.selected_columns(_CFG_WITH_POP)
    assert "popularity" not in [c["id"] for c in cols]
    w = cf.column_weights(_CFG_WITH_POP, {"popularity": 99.0, "burden_weight": 1.0,
                                          "agg_avg_finish|lb=3|m=": 1.0})
    assert "popularity" not in w


def test_predict_response_has_no_popularity_contribution():
    """完了条件: 参加者経路のレスポンスに人気の寄与が現れないこと。"""
    race = _race()
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    preset = {"weights": {"popularity": 99.0, "burden_weight": 1.0,
                          "agg_avg_finish|lb=3|m=": 1.0},
              "confidence_thresholds": {"solid": 1.0, "strong": 0.3,
                                        "scale": svc.ps.CONFIDENCE_SCALE}}
    got = svc.predict_race(race, _CFG_WITH_POP, preset)
    assert "popularity" not in [c["id"] for c in got["columns"]]
    for m in got["marks"]:
        ids = [c["id"] for c in m["contributions"]]
        assert "popularity" not in ids
        assert all("人気" not in c["label"] for c in m["contributions"])
    # 落としたことを黙らず警告する
    warn = next(w for w in got["warnings"] if w["code"] == "excluded_columns_dropped")
    assert "人気" in warn["message"]
    assert warn["columns"][0]["label"] == model.FEATURES["popularity"].label


def test_no_warning_when_config_had_no_popularity():
    race = _race()
    got = svc.predict_race(race, {"step1": [], "step2": [
        {"metric": "agg_avg_finish", "match": [], "lookback": 3}]}, PRESET)
    assert not any(w["code"] == "excluded_columns_dropped" for w in got["warnings"])


def test_popularity_stays_a_base_column_for_the_matrix():
    """基底列としては残すこと (行列キャッシュと列構成指紋の互換性を保つ)。

    参加者経路から外すだけで、425列の構成は変えない。ここが変わると 4.6GB の
    行列キャッシュと保存済みプリセット重みが一斉に無効になる。
    """
    ids = [c["id"] for c in mx._columns(sp.maib_all_specs())]
    assert "popularity" in ids
    assert len(ids) == 425
    # 参加者向けの STEP1 だけが1件少ない
    assert len(sp.maib_participant_step1_specs()) == len(sp.maib_step1_specs()) - 1


def test_backtest_and_leaderboard_cannot_use_popularity():
    """バックテスト・順位表も normalize_config を通るので混入しない。"""
    from builder import leaderboard as lb
    matrix = {"columns": mx._columns([{"key": "popularity"}, {"key": "burden_weight"}]),
              "races": [dict(_race(), date="20260801")]}
    for h in matrix["races"][0]["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    preset = {"weights": {"popularity": 99.0, "burden_weight": 1.0}}
    bt = svc.backtest(matrix, _CFG_WITH_POP, preset, date_from="20260101")
    assert bt["your_ai"]["races"] == 1
    board = lb.build_leaderboard(matrix, preset, configs=[
        {"id": "a", "name": "旧設定AI", "config": _CFG_WITH_POP}])
    entry = next(e for e in board["entries"] if not e["is_baseline"])
    assert entry["races"] == 1                # 動くが人気は使われていない
    # 人気だけの設定は「選べる列ゼロ」になる
    only_pop = {"step1": ["popularity"], "step2": []}
    assert cf.selected_columns(only_pop) == []


def test_empty_selection_is_warned_and_blocks_marks():
    """選べる列が0になる設定は必ず警告すること。

    判断A の除外後に「人気(市場)」だけの旧設定は選択が空になる。列が空だと
    全馬のスコアが 0 で印は無意味なので、開示系の警告 (印を出して良い) だけを
    返して黙って並べてはいけない。
    """
    got = svc.predict_race(_race(), {"step1": ["popularity"], "step2": []}, PRESET)
    codes = {w["code"] for w in got["warnings"]}
    assert "no_columns_selected" in codes
    assert got["n_columns_selected"] == 0
    assert got["n_columns_used"] == 0
    # 除外の開示も併せて出る
    assert "excluded_columns_dropped" in codes

    # 何も選んでいない設定でも同じ
    empty = svc.predict_race(_race(), {"step1": [], "step2": []}, PRESET)
    assert "no_columns_selected" in {w["code"] for w in empty["warnings"]}

    # 1つでも選べていれば出さない
    ok = svc.predict_race(_race(), {"step1": ["burden_weight"], "step2": []}, PRESET)
    assert "no_columns_selected" not in {w["code"] for w in ok["warnings"]}


# ---------------------------------------------------------------------------
# P0: 配信ずれの検出 (完了報告した修正が実機に出ない事故の切り分け)
# ---------------------------------------------------------------------------
def test_version_reports_fresh_on_an_unchanged_tree():
    v = api.version_info()
    assert v["stale"] is False
    assert v["boot_fingerprint"] == v["disk_fingerprint"]
    assert "最新" in v["message"]
    assert v["started_at"]


def test_version_detects_stale_server_code(monkeypatch):
    """builder/*.py が変わったら「再起動が必要」と言うこと。"""
    monkeypatch.setattr(api, "_py_fingerprint", lambda: "deadbeef0000")
    v = api.version_info()
    assert v["stale"] is True
    assert "再起動" in v["message"]
    assert v["disk_fingerprint"] == "deadbeef0000"


def test_version_does_not_demand_restart_for_web_only_changes(monkeypatch):
    """web/ の変更で再起動を要求しないこと。

    web/ はリクエストごとに読み直すのでリロードで反映される。ここを混ぜると
    画面を直すたびに嘘の指示 (再起動してください) を出すことになる。
    """
    monkeypatch.setattr(api, "_web_fingerprint", lambda: "0123456789ab")
    v = api.version_info()
    assert v["stale"] is False
    assert v["web_changed"] is True
    assert "リロード" in v["message"]


def test_version_fingerprint_changes_with_content(tmp_path):
    """指紋が内容に反応すること (ファイル名だけ見ていない)。"""
    a = tmp_path / "x.py"
    a.write_text("one", encoding="utf-8")
    first = api._fingerprint([a])
    a.write_text("two", encoding="utf-8")
    assert api._fingerprint([a]) != first


def test_predict_includes_the_decisive_sentence():
    """P2-4: 決め手の一文はサーバが生成する (UI に表示ロジックを複製しない)。"""
    race = _race()
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    got = svc.predict_race(race, USER_CFG, PRESET)
    top = got["marks"][0]
    assert top["decisive"], "決め手の一文が無い"
    # 項目名が入っている (テンプレだけで中身が無い文にしない)
    assert "「" in top["decisive"]
    # 向きに応じた文になっている (どちらかは必ず含む)
    assert ("決め手です" in top["decisive"]) or ("評価を下げ" in top["decisive"])


def test_decisive_sentence_mentions_a_close_second():
    """2位寄与が僅差なら「も後押ししています」を足すこと。"""
    close = [{"id": "a", "label": "項目A", "contribution": 1.0, "available": True},
             {"id": "b", "label": "項目B", "contribution": 0.9, "available": True}]
    s = svc._decisive_sentence(close, "◎")
    assert "項目A" in s and "項目B" in s and "後押し" in s
    # 差が大きければ2位は出さない
    far = [{"id": "a", "label": "項目A", "contribution": 1.0, "available": True},
           {"id": "b", "label": "項目B", "contribution": 0.1, "available": True}]
    s2 = svc._decisive_sentence(far, "◎")
    assert "項目A" in s2 and "項目B" not in s2


def test_decisive_sentence_keeps_both_clauses_consistent():
    """2文目の向きが1文目と揃っていること。

    「評価を下げています。〜も後押ししています」のような、意味の通らない
    つなぎ方をしないこと (実際に生成されていた)。
    """
    # 下げ + 下げ → 「も評価を下げています」
    dd = [{"id": "a", "label": "A", "contribution": -1.0, "available": True},
          {"id": "b", "label": "B", "contribution": -0.9, "available": True}]
    s = svc._decisive_sentence(dd, "×")
    assert "も評価を下げています" in s and "後押し" not in s
    # 下げ + 上げ → 「ただし〜は評価を上げています」
    du = [{"id": "a", "label": "A", "contribution": -1.0, "available": True},
          {"id": "b", "label": "B", "contribution": 0.9, "available": True}]
    s2 = svc._decisive_sentence(du, "×")
    assert "ただし" in s2 and "評価を上げています" in s2 and "後押し" not in s2
    # 上げ + 下げ → 「ただし〜は評価を下げています」
    ud = [{"id": "a", "label": "A", "contribution": 1.0, "available": True},
          {"id": "b", "label": "B", "contribution": -0.9, "available": True}]
    s3 = svc._decisive_sentence(ud, "◎")
    assert "決め手です" in s3 and "ただし" in s3 and "評価を下げています" in s3


def test_decisive_sentence_is_honest_about_negative_drivers():
    """最大寄与が押し下げなら「決め手」と書かない (嘘をつかない)。"""
    neg = [{"id": "a", "label": "項目A", "contribution": -1.0, "available": True}]
    s = svc._decisive_sentence(neg, "×")
    assert "評価を下げ" in s and "決め手" not in s
    # 使える寄与が無ければ文を作らない
    assert svc._decisive_sentence([], "◎") is None
    assert svc._decisive_sentence(
        [{"id": "a", "label": "A", "contribution": 0.0, "available": False}], "◎") is None


def test_contributions_carry_item_level_coverage():
    """P3: 寄与の各行に項目単位のカバレッジ (値があった頭数) を付す。"""
    race = _race(n=10)
    for i, h in enumerate(race["horses"]):
        h["x"]["burden_weight"] = None if i >= 4 else float(h["num"])
    got = svc.predict_race(race, USER_CFG, PRESET)
    by_id = {c["id"]: c for c in got["marks"][0]["contributions"]}
    full = by_id["agg_avg_finish|lb=3|m="]
    assert full["n_with_value"] == 10 and full["n_runners"] == 10
    # 馬単位の参照走数とは別の軸として両方返る
    assert got["marks"][0]["n_past_runs"] is not None
    assert got["marks"][0]["coverage"]["n_used"] >= 1


def test_predict_returns_result_for_finished_races():
    """P3: 発走済みなら着順を返す (印の代わりに結果を出すため)。"""
    got = svc.predict_race(_race(with_order=True), USER_CFG, PRESET)
    assert got["finished"] is True
    assert [r["order"] for r in got["result"]] == [1, 2, 3]
    assert got["result"][0]["horse_name"]
    # 未確定なら空
    pending = svc.predict_race(_race(with_order=False), USER_CFG, PRESET)
    assert pending["finished"] is False and pending["result"] == []


# ---------------------------------------------------------------------------
# 検証モード (--preview): 過去の開催日で印の画面を確認する
# ---------------------------------------------------------------------------
def test_preview_shows_finished_races_as_upcoming():
    """確定済みレースを発走前として見せること。

    平日や過去日では全レースが終了扱いになり、印の画面をまったく確認できない
    (開催日の発走前という短い時間帯しか触れない)。
    """
    got = api._as_upcoming({"finished": True, "result": [{"order": 1}], "x": 1})
    assert got["finished"] is False
    assert got["result"] == []
    assert got["x"] == 1                       # 他のキーは触らない


def test_preview_is_off_by_default_and_disclosed_when_on(monkeypatch):
    """既定は無効。有効時は必ず画面に出す文言を返すこと (結果を知って見るため)。"""
    monkeypatch.setitem(api._STATE, "preview", False)
    assert api.version_info()["preview"] is False
    monkeypatch.setitem(api._STATE, "preview", True)
    v = api.version_info()
    assert v["preview"] is True
    assert "検証モード" in v["preview_message"]


def test_preview_flag_reaches_predict_and_the_race_list(monkeypatch):
    """predict と一覧の両方で終了扱いを外すこと (片方だけだと矛盾する)。"""
    monkeypatch.setitem(api._STATE, "preview", True)
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    got, status = api.handle_predict({"race_id": "R1", "config": USER_CFG})
    assert status == 200
    assert got["finished"] is False and got["result"] == []
    # 素の predict は確定を隠さない (検証モードは API 層だけの見せ方)
    raw = svc.predict_race(_race("R1"), USER_CFG, PRESET)
    assert raw["finished"] is True


def test_built_dates_lists_current_version_caches(tmp_path, monkeypatch):
    """レース0件のときに案内する「構築済みの日付」を拾えること。"""
    from builder import config as c, matrix_daily as mdmod
    d = tmp_path / "daily"
    d.mkdir()
    (d / f"daily_v{mdmod.DAILY_VERSION}_20260726_abc.json").write_text("{}", encoding="utf-8")
    (d / f"daily_v{mdmod.DAILY_VERSION}_20250705_abc.json").write_text("{}", encoding="utf-8")
    (d / "daily_20240101_old.json").write_text("{}", encoding="utf-8")   # 旧版は無視
    monkeypatch.setattr(c, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    assert api._built_dates() == ["20250705", "20260726"]
