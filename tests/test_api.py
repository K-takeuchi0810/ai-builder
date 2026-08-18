"""configs / predict_service / api のテスト。合成データのみ・CIセーフ。"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import api
from builder import betslip as bslip, configs as cf, config as cfgmod, matrix as mx   # noqa: E402
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
    c = {"name": "A", "step1": ["burden_weight", "jockey_win_rate"], "step2": []}
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


def test_configs_are_isolated_by_owner_in_shared_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    a = cf.save_config({**USER_CFG, "name": "A"}, owner_id="user-a")
    b = cf.save_config({**USER_CFG, "name": "B"}, owner_id="user-b")
    assert a["id"] != b["id"]
    assert [x["id"] for x in cf.list_configs(owner_id="user-a")] == [a["id"]]
    assert cf.get_config(a["id"], owner_id="user-b") is None
    assert cf.rename_config(a["id"], "横取り", owner_id="user-b") is None


def test_leaderboard_cannot_read_another_owners_configs(tmp_path, monkeypatch):
    from builder import leaderboard as lb
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    a = cf.save_config({**USER_CFG, "name": "A"}, owner_id="user-a")
    b = cf.save_config({**USER_CFG, "name": "B"}, owner_id="user-b")
    daily = {"date": "20260801", "races": [_race("R1")]}

    got = lb.build_leaderboard(daily, PRESET, owner_id="user-a",
                               applied={"R1": a["id"]})
    mine = [e for e in got["entries"] if not e["is_baseline"]]
    assert [e["config_id"] for e in mine] == [a["id"]]
    assert all(e["config_id"] != b["id"] for e in mine)


def test_concurrent_config_saves_do_not_lose_other_users(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")

    def save(i):
        return cf.save_config({**USER_CFG, "name": f"AI-{i}"}, owner_id=f"user-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(save, range(20)))
    assert len({x["id"] for x in saved}) == 20
    for i, item in enumerate(saved):
        assert cf.get_config(item["id"], owner_id=f"user-{i}")["name"] == f"AI-{i}"


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
    assert got["marks"] == []
    assert len(got["runners"]) == 12
    assert got["runners"][0]["horse_num"] == "01"


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
    assert len(cat["step2_metrics"]) == len(sp.maib_participant_step2_metrics()) == 8
    assert len(cat["step2_matches"]) == 4 and len(cat["step2_lookbacks"]) == 11
    assert cat["step1"] and all("label" in x for x in cat["step1"])
    assert any("獲得本賞金" in n for n in cat["notes"])


def test_handle_predict_found_and_missing(monkeypatch):
    # _STATE への直代入はテスト間に漏れる (別ファイルの feature_catalog が
    # 合成プリセットを掴み、低サンプル項目が消えて落ちた)。必ず巻き戻す。
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    body, status = api.handle_predict({"race_id": "R1", "config": USER_CFG})
    assert status == 200 and body["marks"][0]["mark"] == "◎"
    body, status = api.handle_predict({"race_id": "nope", "config": USER_CFG})
    assert status == 404 and body["error"] == "race_not_found"


def test_finished_result_can_be_opened_without_a_saved_ai(monkeypatch):
    """終了レースの着順・払戻は、マイAI未作成でも閲覧できる。"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("DONE")]})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    body, status = api.handle_predict({"race_id": "DONE", "result_only": True})
    assert status == 200
    assert body["result_only"] is True and body["finished"] is True
    assert body["result"] and body["warnings"] == []
    assert body["result_pickup_analysis"]["mode"] == "add_one_item"

    monkeypatch.setitem(api._STATE, "daily", {
        "races": [_race("NEXT", with_order=False)]})
    body, status = api.handle_predict({"race_id": "NEXT", "result_only": True})
    assert status == 409 and body["error"] == "race_not_finished"


def test_result_pickup_analysis_names_item_that_moves_winner_into_marks(monkeypatch):
    """正寄与の列名だけでなく、追加後に印圏内へ入ることを順位で検証する。"""
    race = _race("PICK", n=10)
    for i, horse in enumerate(race["horses"], start=1):
        horse["x"] = {
            "jockey_win_rate": float(i),
            "trainer_win_rate": 100.0 if i == 1 else float(i),
        }
    preset = {
        "weights": {"jockey_win_rate": 1.0, "trainer_win_rate": 3.0},
        "confidence_thresholds": PRESET["confidence_thresholds"],
    }
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setitem(api._STATE, "preset", preset)
    body, status = api.handle_predict({
        "race_id": "PICK",
        "config": {"name": "騎手AI", "step1": ["jockey_win_rate"], "step2": []},
    })
    assert status == 200
    analysis = body["result_pickup_analysis"]["horses"]["01"]
    assert analysis["base_rank"] > 5 and analysis["status"] == "into_marks"
    candidate = next(c for c in analysis["candidates"]
                     if c["id"] == "trainer_win_rate")
    assert candidate["to_rank"] <= 5 and candidate["to_rank"] < candidate["from_rank"]
    assert "調教師" in candidate["label"]


def test_result_only_without_ai_finds_single_item_pickup(monkeypatch):
    """マイAI未選択で結果だけを開いても、単独項目で拾えた候補を返す。"""
    race = _race("PICK-ONE", n=10)
    for i, horse in enumerate(race["horses"], start=1):
        horse["x"] = {"trainer_win_rate": 100.0 if i == 1 else float(i)}
    preset = {
        "weights": {"trainer_win_rate": 3.0},
        "confidence_thresholds": PRESET["confidence_thresholds"],
    }
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setitem(api._STATE, "preset", preset)
    body, status = api.handle_predict({"race_id": "PICK-ONE", "result_only": True})
    assert status == 200 and body["marks"] == []
    analysis = body["result_pickup_analysis"]["horses"]["01"]
    assert analysis["base_rank"] is None and analysis["status"] == "into_marks"
    assert analysis["candidates"][0]["id"] == "trainer_win_rate"
    assert analysis["candidates"][0]["to_rank"] <= 5
    review = body["result_review_ai"]
    assert review and review["mode"] == "retrospective_ai"
    assert review["items"][0]["id"] == "trainer_win_rate"
    assert review["placed_in_marks"] >= 1


def test_result_item_review_summarizes_day_with_track_surface_and_distance(monkeypatch):
    """36レース一覧用APIは、各レースの条件と上位馬の代表項目をまとめる。"""
    finished = _race("DONE", n=10)
    finished.update({
        "race_num": 1, "start_time": "09:50", "race_title": "2歳未勝利",
        "seg": {"track": "01", "surface": "dirt", "distance": 1700,
                "condition": "good"},
    })
    for i, horse in enumerate(finished["horses"], start=1):
        horse["x"] = {"trainer_win_rate": 100.0 if i == 1 else float(i)}
    waiting = _race("WAIT", n=10, with_order=False)
    waiting.update({
        "race_num": 2, "start_time": "10:20", "race_title": "2歳未勝利",
        "seg": {"track": "01", "surface": "turf", "distance": 1200,
                "condition": "good"},
    })
    daily = {"date": "20260801", "columns": [], "races": [finished, waiting]}
    preset = {
        "weights": {"trainer_win_rate": 3.0},
        "confidence_thresholds": PRESET["confidence_thresholds"],
    }
    monkeypatch.setitem(api._STATE, "date", "20260802")
    monkeypatch.setitem(api._STATE, "preset", preset)
    monkeypatch.setitem(api._STATE, "_result_item_review_cache", {})
    monkeypatch.setattr(api, "_daily_for_date", lambda date: daily if date == "20260801" else {})

    body, status = api.handle_result_item_review("20260801")
    assert status == 200
    assert body["race_count"] == 2 and body["finished_count"] == 1
    first = next(r for r in body["races"] if r["race_id"] == "DONE")
    assert first["track_label"] and first["surface_label"]
    assert first["distance"] == 1700 and len(first["top3"]) == 3
    assert first["top3"][0]["candidate"]["id"] == "trainer_win_rate"
    assert body["mode"] == "retrospective_ai"
    assert first["review_ai"]["mode"] == "retrospective_ai"
    assert first["review_ai"]["items"][0]["id"] == "trainer_win_rate"
    assert first["review_ai"]["placed"][0]["ai_rank"] <= 5
    assert next(r for r in body["races"] if r["race_id"] == "WAIT")["top3"] == []


def test_live_refresh_rolls_an_always_on_server_to_today(monkeypatch):
    old_daily = {"date": "20260731", "races": [_race("OLD", date="20260731")]}
    new_daily = {"date": "20260801", "races": [_race("NEW", date="20260801")]}
    refreshed = {**new_daily, "live_updated_at": "2026-08-01T09:00:00+09:00"}
    monkeypatch.setitem(api._STATE, "date", "20260731")
    monkeypatch.setitem(api._STATE, "daily", old_daily)
    monkeypatch.setitem(api._STATE, "specs", [{"key": "popularity"}])
    monkeypatch.setitem(api._STATE, "follow_today", True)
    monkeypatch.setitem(api._STATE, "auto_build", True)
    monkeypatch.setattr(api.md, "load_daily", lambda date, specs: new_daily)
    monkeypatch.setattr(api.md, "refresh_live", lambda daily: refreshed)

    api._refresh_current_daily(force=True, today="20260801")

    assert api._STATE["date"] == "20260801"
    assert api._STATE["daily"]["races"][0]["race_id"] == "NEW"
    assert api._STATE["daily"]["live_updated_at"] == "2026-08-01T09:00:00+09:00"


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


def test_handle_leaderboard_requires_daily_matrix(monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": []})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    body, status = api.handle_leaderboard()
    assert status == 409 and body["error"] == "daily_matrix_not_built"


def test_handle_backtest_requires_data(monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": []})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    monkeypatch.delitem(api._STATE, "backtest_matrix", raising=False)
    body, status = api.handle_backtest({"config": USER_CFG})
    assert status == 409 and body["error"] == "no_backtest_data"

    monkeypatch.setitem(api._STATE, "backtest_matrix",
                        {"columns": [], "races": [_race("A", date="20250801")]})
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
    # 基底列は維持し、参加者向けだけ監査不合格項目を除く
    assert len(sp.maib_participant_step1_specs()) == \
        len(sp.maib_step1_specs()) - len(
            sp.PARTICIPANT_UNAVAILABLE_KEYS & set(sp.MAIB_STEP1_KEYS))


def test_failed_holdout_items_are_not_offered_or_restored():
    cat = api.feature_catalog()
    offered1 = {x["key"] for x in cat["step1"]}
    offered2 = {x["metric"] for x in cat["step2_metrics"]}
    assert not offered1.intersection(sp.PARTICIPANT_RETIRED_KEYS)
    assert "agg_gain_first_to_last" not in offered2
    old = {"step1": ["draw_position", "burden_weight"], "step2": [
        {"metric": "agg_gain_first_to_last", "match": [], "lookback": 3}]}
    assert cf.normalize_config(old)["step1"] == ["burden_weight"]
    assert cf.normalize_config(old)["step2"] == []
    assert set(cf.excluded_in_config(old)) == {"draw_position", "agg_gain_first_to_last"}


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
    # 下げ + 下げ → 「も評価を下げています」(上位の印。△× は順位文脈に切り替わる)
    dd = [{"id": "a", "label": "A", "contribution": -1.0, "available": True},
          {"id": "b", "label": "B", "contribution": -0.9, "available": True}]
    s = svc._decisive_sentence(dd, "▲")
    assert "も評価を下げています" in s and "後押し" not in s
    # 下げ + 上げ → 「ただし〜は評価を上げています」
    du = [{"id": "a", "label": "A", "contribution": -1.0, "available": True},
          {"id": "b", "label": "B", "contribution": 0.9, "available": True}]
    s2 = svc._decisive_sentence(du, "▲")
    assert "ただし" in s2 and "評価を上げています" in s2 and "後押し" not in s2
    # 上げ + 下げ → 「ただし〜は評価を下げています」
    ud = [{"id": "a", "label": "A", "contribution": 1.0, "available": True},
          {"id": "b", "label": "B", "contribution": -0.9, "available": True}]
    s3 = svc._decisive_sentence(ud, "◎")
    assert "決め手です" in s3 and "ただし" in s3 and "評価を下げています" in s3


def test_decisive_sentence_is_honest_about_negative_drivers():
    """最大寄与が押し下げなら「決め手」と書かない (嘘をつかない)。"""
    neg = [{"id": "a", "label": "項目A", "contribution": -1.0, "available": True}]
    s = svc._decisive_sentence(neg, "▲", rank=3, n_runners=12)
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
    got = api._as_upcoming({"finished": True, "started": True,
                            "result": [{"order": 1}], "x": 1})
    assert got["finished"] is False and got["started"] is False
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


def test_built_dates_lists_versioned_caches_across_upgrades(tmp_path, monkeypatch):
    """形式更新前の開催日も、結果・購入履歴の候補として拾うこと。"""
    from builder import config as c, matrix_daily as mdmod
    d = tmp_path / "daily"
    d.mkdir()
    (d / f"daily_v{mdmod.DAILY_VERSION}_20260726_abc.json").write_text("{}", encoding="utf-8")
    (d / f"daily_v{mdmod.DAILY_VERSION}_20250705_abc.json").write_text("{}", encoding="utf-8")
    (d / "daily_v4_20260808_abc.json").write_text("{}", encoding="utf-8")
    (d / "daily_20240101_old.json").write_text("{}", encoding="utf-8")   # 旧版は無視
    monkeypatch.setattr(c, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    assert api._built_dates() == ["20250705", "20260726", "20260808"]


# ---------------------------------------------------------------------------
# F2: 枠番はデータが正 (馬番からの計算は禁止)
# ---------------------------------------------------------------------------
def test_waku_comes_from_the_data_not_from_the_horse_number():
    """枠番は DB 由来の値をそのまま返すこと。

    JRA の枠割は頭数依存で、7頭立てでは馬番=枠番になる。実測で ceil(馬番/2)
    は 7頭立ての 6/7 件を外した。UI 側の導出は原理的に不可能。
    """
    race = _race(n=7)
    for i, h in enumerate(race["horses"], start=1):
        h["waku"] = i                          # 7頭立て: 枠番 = 馬番
        h["x"]["burden_weight"] = float(i)
    got = svc.predict_race(race, USER_CFG, PRESET)
    by_num = {m["horse_num"]: m["waku"] for m in got["marks"]}
    assert by_num == {f"{i:02d}": i for i in range(1, 8)}


def test_waku_is_none_when_absent():
    """データに無ければ None (UI は色を付けない)。"""
    race = _race(n=8)
    for h in race["horses"]:
        h.pop("waku", None)
        h["x"]["burden_weight"] = float(h["num"])
    got = svc.predict_race(race, USER_CFG, PRESET)
    assert all(m["waku"] is None for m in got["marks"])


def test_matrix_daily_waku_parsing():
    """waku_num の解釈: 1〜8 の数字のみ採用、それ以外は None。"""
    from builder import matrix_daily as mdmod
    assert mdmod._waku({"waku_num": "3"}) == 3
    assert mdmod._waku({"waku_num": 8}) == 8
    assert mdmod._waku({"waku_num": "0"}) is None       # 枠は1始まり
    assert mdmod._waku({"waku_num": "9"}) is None
    assert mdmod._waku({"waku_num": ""}) is None
    assert mdmod._waku({"waku_num": None}) is None
    assert mdmod._waku({}) is None


# ---------------------------------------------------------------------------
# F9: 未知config / 使用項目0 で印を返さない (fail-closed)
# ---------------------------------------------------------------------------
def test_unknown_config_id_returns_404(tmp_path, monkeypatch):
    """未知の config_id は 404。空 config に落として印を返してはいけない。"""
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    got, status = api.handle_predict({"race_id": "R1", "config_id": "nope"})
    assert status == 404 and got["error"] == "config_not_found"
    assert "marks" not in got


def test_known_config_id_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    saved = cf.save_config(USER_CFG)
    race = _race("R1")
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    got, status = api.handle_predict({"race_id": "R1", "config_id": saved["id"]})
    assert status == 200 and got["marks"]


def test_no_marks_when_no_column_is_usable():
    """使える項目0なら marks を空にすること。

    全馬スコア0だと順位は入力順のままで、それに ◎○▲△× を付けると
    「入力順を順位として提示する」ことになる。UI のガードだけに頼らない。
    """
    # 空選択
    empty = svc.predict_race(_race(), {"step1": [], "step2": []}, PRESET)
    assert empty["marks"] == []
    assert "no_columns_selected" in {w["code"] for w in empty["warnings"]}
    # 人気だけ (判断A の除外で空になる)
    pop = svc.predict_race(_race(), {"step1": ["popularity"], "step2": []}, PRESET)
    assert pop["marks"] == []
    # 重みが無い
    nw = svc.predict_race(_race(), USER_CFG, {"weights": {}})
    assert nw["marks"] == []
    assert "no_preset_weights" in {w["code"] for w in nw["warnings"]}
    # 全項目がゲート落ち
    race = _race(n=12)
    for i, h in enumerate(race["horses"]):
        if i >= 2:
            h["x"] = {k: None for k in h["x"]}
    gated = svc.predict_race(race, USER_CFG, PRESET)
    assert gated["marks"] == []


def test_marks_are_returned_when_a_column_is_usable():
    """逆に、使える項目が1つでもあれば印は返る (過剰に塞がない)。"""
    race = _race()
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    got = svc.predict_race(race, USER_CFG, PRESET)
    assert len(got["marks"]) == len(race["horses"])
    assert got["marks"][0]["mark"] == "◎"


def test_debut_race_is_explicitly_capped_as_reference():
    race = _race()
    race["race_class"] = "新馬"
    for h in race["horses"]:
        h["n_past_runs"] = 0
    got = svc.predict_race(race, USER_CFG, PRESET)
    assert got["marks"]
    assert got["history_reliability"]["profile"] == "debut"
    assert got["history_reliability"]["recommended_preset"] == "debut"
    assert got["confidence"]["label"] == "参考"
    assert got["confidence"]["downgraded"] is True
    assert "limited_history" in {w["code"] for w in got["warnings"]}


def test_low_history_maiden_cannot_show_strong_confidence():
    race = _race()
    race["race_class"] = "未勝利"
    for h in race["horses"]:
        h["n_past_runs"] = 1
    got = svc.predict_race(race, USER_CFG, PRESET)
    assert got["history_reliability"]["profile"] == "limited"
    assert got["history_reliability"]["recommended_preset"] == "maiden"
    assert got["confidence"]["label"] not in ("鉄板級", "有力")


# ---------------------------------------------------------------------------
# F6: 順位規則は labels.py の静的文字列
# ---------------------------------------------------------------------------
def test_ranking_rule_is_static_and_served_with_the_catalog():
    """board が空でも読めるよう、規則文を選択肢と同じ経路で供給すること。"""
    from builder import labels as lbl, leaderboard as lb
    cat = api.feature_catalog()
    assert cat["ranking_rule"] == lbl.RANKING_RULE
    for part in ("◎的中率", "出し抜", "複勝率", "同順位"):
        assert part in lbl.RANKING_RULE, part
    # leaderboard も同じ文字列を使う (二重管理しない)
    board = lb.build_leaderboard({"races": []}, PRESET, configs=[])
    assert board["ranking_rule"] == lbl.RANKING_RULE


# ---------------------------------------------------------------------------
# R4: 印の説明を順位文脈で書く
# ---------------------------------------------------------------------------
def test_low_mark_explanation_starts_from_the_rank():
    """×印 (寄与が押し下げのみ) の説明が順位から始まること。

    以前は「評価を下げています」+ 下げた内訳しか出ず、「悪い馬になぜ印が
    付くのか」が説明されていなかった。印は絶対評価ではなく相対順位。
    """
    neg = [{"id": "a", "label": "近走の調子", "contribution": -0.8, "available": True}]
    for mark, rank in (("△", 4), ("×", 5)):
        s = svc._decisive_sentence(neg, mark, rank=rank, n_runners=12)
        assert s.startswith(f"12頭中{rank}番目の評価です。"), s
        assert "相対的に上でした" in s, s
        assert "決め手" not in s


def test_top_mark_explanation_keeps_the_decisive_wording():
    """上位の印 (◎○) は現行の「決め手です」を踏襲すること。"""
    pos = [{"id": "a", "label": "平均着順", "contribution": 1.2, "available": True}]
    s = svc._decisive_sentence(pos, "◎", rank=1, n_runners=12)
    assert "決め手です" in s and "「平均着順」" in s


def test_thin_past_runs_is_flagged_on_the_mark():
    """過去走が少ない馬は印の側で開示する (whyカードを開かなくても見える)。"""
    race = _race(n=8)
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    race["horses"][0]["n_past_runs"] = 1
    race["horses"][1]["n_past_runs"] = 8
    got = svc.predict_race(race, USER_CFG, PRESET)
    by = {m["horse_num"]: m for m in got["marks"]}
    assert by["01"]["few_past_runs"] is True
    assert by["02"]["few_past_runs"] is False
    assert svc.MIN_PAST_RUNS == 3


# ---------------------------------------------------------------------------
# R5: 出走情報と平易表現
# ---------------------------------------------------------------------------
def test_marks_carry_the_entry_information():
    """騎手・斤量・調教師・性齢・馬体重が印に載ること。

    AI が「騎手の成績」を根拠に印を打つのに騎手名を出していなかった。
    """
    race = _race(n=6)
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
        h.update({"jockey": "テスト騎手", "burden_weight": 55.0, "trainer": "テスト調教師",
                  "sex_age": "牡4", "horse_weight": 486, "horse_weight_change": 4})
    got = svc.predict_race(race, USER_CFG, PRESET)
    m = got["marks"][0]
    assert m["jockey"] == "テスト騎手" and m["burden_weight"] == 55.0
    assert m["trainer"] == "テスト調教師" and m["sex_age"] == "牡4"
    assert m["horse_weight"] == 486 and m["horse_weight_change"] == 4


def test_contributions_carry_plain_wording_not_raw_z():
    """寄与に平易表現が付き、生の z をそのまま見せなくて済むこと。"""
    from builder import labels as lbl
    race = _race(n=10)
    for h in race["horses"]:
        h["x"]["burden_weight"] = float(h["num"])
    got = svc.predict_race(race, USER_CFG, PRESET)
    cs = got["marks"][0]["contributions"]
    assert cs
    for c in cs:
        if c["available"]:
            assert c["value_text"] in [b[1] for b in lbl.Z_BANDS], c
        else:
            assert c["value_text"] is None


def test_prediction_excludes_cancelled_horse_and_reports_it():
    race = _race(n=6)
    scratched = race["horses"][1]
    scratched.update({"scratched": True, "scratch_status": "競走除外"})
    got = svc.predict_race(race, USER_CFG, PRESET)
    assert scratched["num"] not in {m["horse_num"] for m in got["marks"]}
    assert scratched["num"] not in {h["horse_num"] for h in got["runners"]}
    assert got["scratched_horses"] == [{
        "horse_num": scratched["num"], "horse_name": scratched["name"],
        "label": "競走除外",
    }]


def test_plain_level_bands_are_ordered_and_single_sourced():
    """5段変換が labels.py の単一辞書で、境界が単調であること。"""
    from builder import labels as lbl
    zs = [2.0, 0.5, 0.0, -0.5, -2.0]
    got = [lbl.plain_level(z) for z in zs]
    assert got == ["出走馬の中でかなり上", "出走馬の中で上", "平均的",
                   "出走馬の中で下", "出走馬の中でかなり下"]
    assert lbl.plain_level(None) is None
    # 境界は降順 (単調)
    ths = [t for t, _ in lbl.Z_BANDS]
    assert ths == sorted(ths, reverse=True)


# ---------------------------------------------------------------------------
# R2/R3/R7: レース表示・条件別成績・適用AI
# ---------------------------------------------------------------------------
def test_race_title_and_class_are_separate_fields():
    """名称スロットに条件を焼き込まないこと (特別戦で条件が消えるのを防ぐ)。"""
    from builder import matrix_daily as mdmod
    named = {"race_name": "羊ヶ丘特別", "race_short10": "", "race_short6": ""}
    assert mdmod._race_title(named) == "羊ヶ丘特別"
    assert mdmod._race_title({"race_name": "", "race_short10": ""}) is None


def test_race_class_labels_prefer_the_grade():
    """重賞グレードがあればクラスより前に出すこと。"""
    from builder import raceclass as rc
    assert rc.race_class_label("999", "A") == "G1"
    assert rc.race_class_label("999", "L") == "リステッド"
    assert rc.race_class_label("999", "E") == "オープン"   # E は特別戦なのでクラスを使う
    assert rc.race_class_label("703", "") == "未勝利"
    assert rc.race_class_label("005", "") == "1勝クラス"
    assert rc.race_class_label("zzz", "") is None          # 未知は捏造しない


def test_race_condition_parser_offsets():
    """競走条件コードの位置と切り出しが固定されていること。"""
    from builder import raceclass as rc
    rec = bytearray(b" " * 700)
    rec[rc.GRADE_POS] = ord("C")
    rec[rc.COND_START:rc.COND_START + 15] = b"000000016016016"
    got = rc.parse_conditions(bytes(rec))
    assert got["grade"] == "C"
    assert got["conditions"] == ["000", "000", "016", "016", "016"]
    assert got["class_code"] == "016"
    assert rc.race_class_label(got["class_code"], got["grade"]) == "G3"
    # 短いレコードは空 dict (捏造しない)
    assert rc.parse_conditions(b"RA7") == {}


def test_backtest_reports_condition_breakdown():
    """芝ダート・距離帯の内訳が出て、少数条件は数値を出さないこと。"""
    races = []
    for i in range(3):
        r = _race(f"R{i}", date="20250801")
        r["seg"] = {"surface": "turf", "distance": 1200}
        for h in r["horses"]:
            h["x"]["burden_weight"] = float(h["num"])
        races.append(r)
    bt = svc.backtest({"columns": [], "races": races}, USER_CFG, PRESET,
                      date_from="20250101")
    keys = {c["key"] for c in bt["by_condition"]}
    assert {"turf", "short", "turf:short"} <= keys
    for c in bt["by_condition"]:
        # 3レースしかないので数値を出さない判定になる
        assert c["enough"] is False and c["races"] == 3
    assert bt["min_races_for_rate"] == svc.MIN_RACES_FOR_RATE


def test_distance_bands_are_fixed_and_coarse():
    """距離帯は事前固定の3分割のみ (細分化は R3-c で凍結)。"""
    assert svc.distance_band(1200) == "short"
    assert svc.distance_band(1600) == "mile"
    assert svc.distance_band(2400) == "long"
    assert svc.distance_band(None) is None
    assert len(svc.DISTANCE_BANDS) == 3


def test_leaderboard_can_scope_to_the_applied_ai():
    """適用AIの対応を渡すと、そのレースに使ったAIだけ集計すること。"""
    from builder import leaderboard as lb
    races = []
    for i in range(2):
        r = _race(f"R{i}")
        for h in r["horses"]:
            h["x"]["burden_weight"] = float(h["num"])
        races.append(r)
    daily = {"races": races}
    cfgs = [{"id": "a", "name": "AI-A", "config": USER_CFG},
            {"id": "b", "name": "AI-B", "config": USER_CFG}]
    # 指定なし: 両AIが全レースを集計
    full = lb.build_leaderboard(daily, PRESET, configs=cfgs)
    by = {e["name"]: e for e in full["entries"]}
    assert by["AI-A"]["races"] == 2 and by["AI-B"]["races"] == 2
    assert full["scoped_to_applied"] is False
    # 指定あり: R0 は A、R1 は B
    scoped = lb.build_leaderboard(daily, PRESET, configs=cfgs,
                                  applied={"R0": "a", "R1": "b"})
    by2 = {e["name"]: e for e in scoped["entries"]}
    assert by2["AI-A"]["races"] == 1 and by2["AI-B"]["races"] == 1
    assert scoped["scoped_to_applied"] is True
    # ベースラインは全レース
    assert next(e for e in scoped["entries"] if e["is_baseline"])["races"] == 2


def test_config_list_rename_and_duplicate(tmp_path, monkeypatch):
    """複数マイAIの一覧・改名・複製 (R3-b)。"""
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    a = cf.save_config({"name": "AI-A", "step1": ["burden_weight"], "step2": []})
    cf.save_config({"name": "AI-B", "step1": ["jockey_win_rate"], "step2": []})
    names = [e["name"] for e in cf.list_configs()]
    assert names == ["AI-A", "AI-B"]
    assert all(e["n_items"] == 1 for e in cf.list_configs())
    # 複製は別 id
    dup = cf.duplicate_config(a["id"])
    assert dup["id"] != a["id"] and "コピー" in dup["name"]
    # 改名は内容とバージョンを変えない
    before = cf.get_config(a["id"])["version"]
    cf.rename_config(a["id"], "改名")
    assert cf.get_config(a["id"])["version"] == before
    assert cf.get_config(a["id"])["name"] == "改名"
    assert cf.rename_config("nope", "x") is None
    assert cf.duplicate_config("nope") is None


def test_config_names_are_unique_and_archiving_is_reversible(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    a = cf.save_config({"name": "マイAI", "step1": ["burden_weight"], "step2": []},
                       owner_id="user-a")
    b = cf.save_config({"name": "マイAI", "step1": ["draw_position"], "step2": []},
                       owner_id="user-a")
    assert [x["name"] for x in cf.list_configs(owner_id="user-a")] == ["マイAI", "マイAI 2"]

    assert cf.archive_config(a["id"], owner_id="user-b") is None
    assert cf.archive_config(a["id"], owner_id="user-a")["archived"] is True
    assert [x["id"] for x in cf.list_configs(owner_id="user-a")] == [b["id"]]
    all_configs = cf.list_configs(owner_id="user-a", include_archived=True)
    assert {x["id"] for x in all_configs} == {a["id"], b["id"]}
    restored = cf.archive_config(a["id"], archived=False, owner_id="user-a")
    assert restored["archived"] is False


def test_applied_ai_mapping_is_persisted_per_owner_and_date(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    cf.remember_applied("user-a", "20260801", "R1", "cfg-a")
    cf.remember_applied("user-b", "20260801", "R1", "cfg-b")
    assert cf.applied_configs("user-a", "20260801") == {"R1": "cfg-a"}
    assert cf.applied_configs("user-b", "20260801") == {"R1": "cfg-b"}
    assert cf.applied_configs("user-a", "20260802") == {}


def test_legacy_configs_are_preserved_but_only_one_remains_active(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PRESET_WEIGHTS_PATH", tmp_path / "p.json")
    older = cf.save_config({"name": "参加者AI 1", "step1": ["draw_position"], "step2": []})
    current = cf.save_config({"name": "参加者AI 1", "step1": ["burden_weight"], "step2": []})
    cf.save_config({"name": "参加者AI 1", "step1": ["burden_weight"], "step2": []},
                   config_id=current["id"])

    migrated = cf.migrate_legacy_configs("role:admin")
    assert migrated == {"migrated": 2, "archived": 1, "active_id": current["id"]}
    active = cf.list_configs(owner_id="role:admin")
    assert len(active) == 1 and active[0]["id"] == current["id"]
    assert active[0]["name"] == "マイAI 1"
    all_configs = cf.list_configs(owner_id="role:admin", include_archived=True)
    assert len(all_configs) == 2
    assert next(x for x in all_configs if x["id"] == older["id"])["archived"] is True


# ---------------------------------------------------------------------------
# POST /api/betslip — 参加者が組んだ買い目
# ---------------------------------------------------------------------------
def _runners(n=None):
    hs = api._STATE["daily"]["races"][0]["horses"]
    nums = [h["num"] for h in hs]
    return nums[:n] if n else nums


def test_betslip_builds_what_the_participant_asked_for(monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    a, b, c = _runners(3)
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "umaren", "mode": "box", "groups": [[a, b, c]]},
        {"type": "umatan", "mode": "nagashi_both", "groups": [[a], [b, c]]},
    ]})
    assert status == 200, got
    by = {t["key"]: t for t in got["slip"]}
    assert by["umaren"]["n"] == 3                 # C(3,2)
    assert by["umatan"]["n"] == 4                 # 軸の1着2着 × 相手2頭
    assert got["total"] == 7
    # 表記の正本はサーバ。矢印がここで確定する
    assert all("→" in x for x in by["umatan"]["texts"])
    assert all("→" not in x for x in by["umaren"]["texts"])
    assert len(got["text"].splitlines()) == got["total"]
    assert got["note"]


def test_betslip_builds_a_three_horse_formation(monkeypatch):
    """フォーメーションが API 経由で組めること (段数はサーバが決める)。"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    a, b, c, d = _runners(4)
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "sanrentan", "mode": "formation", "groups": [[a], [b, c], [c, d]]},
    ]})
    assert status == 200, got
    t = got["slip"][0]
    assert t["n"] == 3, t["texts"]        # (b,c)(b,d)(c,d) から重なりを除く
    assert all(x.count("→") == 2 for x in t["texts"])


def test_betslip_wakuren_uses_frames_and_allows_the_zoro(monkeypatch):
    """枠連は枠で選び、2頭以上いる枠のゾロ目を含むこと。"""
    race = _race("R1")
    for i, h in enumerate(race["horses"]):
        h["waku"] = 1 if i < 2 else 2         # 枠1に2頭、枠2に残り
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "wakuren", "mode": "box", "groups": [["1", "2"]]}]})
    assert status == 200, got
    texts = got["slip"][0]["texts"]
    assert "1-1" in texts, texts           # 枠1は2頭いるので成立
    assert "1-2" in texts
    if len(race["horses"]) - 2 < 2:
        assert "2-2" not in texts


def test_betslip_rejects_horses_not_in_the_race(monkeypatch):
    """出走していない馬番は組まない。**誤った馬券を作らせない最後の門。**"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["99"]]}]})
    assert status == 200, got              # 1件だけの失敗は skipped で返す
    assert got["slip"] == []
    assert "99" in got["skipped"][0]["reason"]


def test_betslip_reports_the_entry_it_could_not_build(monkeypatch):
    """1件の指定違いで他の買い目まで消さない。位置と理由を返す。"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    a, b = _runners(2)
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "umaren", "mode": "nagashi", "groups": [[a], []]},
        {"type": "tan", "mode": "each", "groups": [[b]]},
    ]})
    assert status == 200, got
    assert [t["key"] for t in got["slip"]] == ["tan"]
    assert got["skipped"][0]["index"] == 0
    assert got["skipped"][0]["reason"]


def test_betslip_rejects_a_non_list_selection(monkeypatch):
    """旧形式 (dict) や壊れた形は 400。**500 で接続を落とさない。**"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    got, status = api.handle_betslip(
        {"race_id": "R1", "selection": {"horses": ["01"]}})
    assert status == 400 and got["error"] == "invalid_selection"


def test_betslip_needs_a_known_race(monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    got, status = api.handle_betslip({"race_id": "nope", "selection": []})
    assert status == 404 and got["error"] == "race_not_found"


def test_betslip_needs_the_daily_matrix(monkeypatch):
    monkeypatch.delitem(api._STATE, "daily", raising=False)
    got, status = api.handle_betslip({"race_id": "R1", "selection": []})
    assert status == 409 and got["error"] == "daily_matrix_not_built"


def test_betslip_returns_odds_for_the_selected_point(monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    got, status = api.handle_betslip({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["01"]], "amount_yen": 200},
    ]})
    assert status == 200, got
    point = got["slip"][0]["odds"][0]
    assert point["available"] is True and point["low"] == 3.0
    assert got["odds_note"]


def test_smappy_qr_sends_expanded_points_and_amounts(monkeypatch):
    race = _race("R1", date="20260801")
    race["seg"] = {"track": "04"}
    race["race_num"] = "1"
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setattr(api, "_live_feed_status", lambda _date=None: {
        "fresh": True, "checked_at": "2026-08-01T09:00:00+09:00",
        "age_seconds": 20, "max_age_seconds": 90,
    })
    captured = {}

    def fake_create_qr(**kwargs):
        captured.update(kwargs)
        return {"qr_png": "data:image/png;base64,TEST", "created_at": "2026-07-31T22:00:00",
                "points": len(kwargs["points"]), "total_yen": 600, "verified": True}

    monkeypatch.setattr(api.smappy, "create_qr", fake_create_qr)
    got, status = api.handle_smappy_qr({"race_id": "R1", "selection": [
        {"type": "umaren", "mode": "box", "groups": [["01", "02", "03"]],
         "amounts_yen": [100, 200, 300]},
    ]})
    assert status == 200, got
    assert captured["date"] == "20260801"
    assert captured["track_code"] == "04" and captured["race_num"] == 1
    assert [(p.bet_type, p.combo, p.amount_yen) for p in captured["points"]] == [
        ("umaren", (1, 2), 100), ("umaren", (1, 3), 200),
        ("umaren", (2, 3), 300),
    ]
    assert got["verified"] is True and got["total_yen"] == 600
    assert len(got["items"]) == 3


def test_smappy_qr_blocks_when_0b14_confirmation_is_stale(monkeypatch):
    race = _race("R1", date="20260801")
    race["seg"] = {"track": "04"}
    race["race_num"] = "1"
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setattr(api, "_live_feed_status", lambda _date=None: {
        "fresh": False, "checked_at": "2026-08-01T09:00:00+09:00",
        "age_seconds": 121, "max_age_seconds": 90,
    })
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(api.smappy, "create_qr", should_not_run)
    got, status = api.handle_smappy_qr({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["01"]], "amount_yen": 100},
    ]})
    assert status == 503 and got["error"] == "live_data_stale"
    assert called is False


def test_smappy_qr_never_partially_sends_invalid_selection(monkeypatch):
    race = _race("R1", date="20260801")
    race["seg"] = {"track": "04"}
    race["race_num"] = "1"
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(api.smappy, "create_qr", should_not_run)
    got, status = api.handle_smappy_qr({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["99"]], "amount_yen": 100},
    ]})
    assert status == 400 and got["error"] == "invalid_selection"
    assert called is False


def test_smappy_qr_stops_before_jra_after_the_scheduled_start(monkeypatch):
    race = _race("R1", date="20260802")
    race["start_time"] = "10:00"
    race["seg"] = {"track": "01"}
    race["race_num"] = "1"
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(api.smappy, "create_qr", should_not_run)
    monkeypatch.setattr(api, "_race_sales_closed", lambda _race: True)
    got, status = api.handle_smappy_qr({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["01"]], "amount_yen": 100},
    ]})
    assert status == 409 and got["error"] == "race_closed"
    assert "10:00" in got["message"] and called is False


def test_purchase_can_be_recorded_after_start_without_calling_jra(tmp_path, monkeypatch):
    race = _race("R1", date="20260802")
    race["start_time"] = "10:00"
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setattr(api.purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(api.smappy, "create_qr", should_not_run)
    got, status = api.handle_purchase_record({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["01"]], "amount_yen": 200},
    ]}, owner_id="alice")
    assert status == 201, got
    assert got["purchase_status"] == "purchased" and got["total_yen"] == 200
    assert got["registered_before_start"] is False
    assert called is False


def test_purchase_record_rejects_partially_invalid_selection(tmp_path, monkeypatch):
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    monkeypatch.setattr(api.purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    got, status = api.handle_purchase_record({"race_id": "R1", "selection": [
        {"type": "tan", "mode": "each", "groups": [["01"]]},
        {"type": "tan", "mode": "each", "groups": [["99"]]},
    ]}, owner_id="alice")
    assert status == 400 and got["error"] == "invalid_selection"
    assert api.purchases.list_for_date("alice", "", [])["entries"] == []


def test_catalog_serves_every_bet_type_with_its_input_rows():
    """券種・買い方・**入力欄の並び**まで API が返すこと。

    UI が段数や見出しを自前で決めると、フォーメーションの段数のような構造が
    2箇所に散る。ここが唯一の出どころ。
    """
    from builder import labels as lbl
    cat = api.feature_catalog()
    assert [t["key"] for t in cat["bet_types"]] == [t["key"] for t in bslip.BET_TYPES]
    for t in cat["bet_types"]:
        assert t["unit"] in ("horse", "frame")
        assert t["modes"], t["key"]
        for m in t["modes"]:
            assert m["label"] == lbl.bet_mode_label(m["key"])
            assert m["desc"], (t["key"], m["key"])
            assert m["groups"], (t["key"], m["key"])
            # 見出しが英字のまま出ないこと
            for g in m["groups"]:
                assert g["label"] and not g["label"].isascii(), (t["key"], m["key"], g)
            if m["key"] == "formation":
                assert len(m["groups"]) == t["size"], (t["key"], m["key"])
    assert cat["bet_slip_note"] == lbl.BET_SLIP_NOTE
    assert cat["bet_zoro_note"] == lbl.ZORO_NOTE


def test_predict_returns_a_starting_selection(monkeypatch):
    """UI が編集を始める起点をサーバが与えること (UI で既定を作らない)。"""
    monkeypatch.setitem(api._STATE, "daily", {"races": [_race("R1")]})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    got, status = api.handle_predict({"race_id": "R1", "config": USER_CFG})
    assert status == 200
    sel = got["bet_selection"]
    assert isinstance(sel, list) and sel
    for e in sel:
        assert e["type"] in bslip.BY_KEY and e["mode"] and e["groups"]
    # 既定で組んだ結果が bet_slip と一致する
    slip, skipped = bslip.build_custom(sel)
    assert skipped == []
    assert [t["n"] for t in slip] == [t["n"] for t in got["bet_slip"]]


def test_race_context_uses_the_selected_race_and_backtest_history(monkeypatch):
    race = _race("R1")
    source = {"races": [_race("OLD", date="20260701")], "columns": []}
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    monkeypatch.setitem(api._STATE, "backtest_matrix", source)
    monkeypatch.setitem(api._STATE, "_context_trend_cache", {})
    monkeypatch.setattr(api, "_refresh_current_daily", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_daily_for_date", lambda _date: {})
    captured = {}

    def fake_analyze(got_source, got_race):
        captured.update(source=got_source, race=got_race)
        return {"available": True, "items": [{"key": "burden_weight"}]}

    monkeypatch.setattr(api.context_trends, "analyze", fake_analyze)
    got, status = api.handle_context_trends("R1")
    assert status == 200 and got["available"] is True
    assert captured == {"source": source, "race": race}


def test_race_context_merges_previous_day_final_results(monkeypatch):
    race = _race("R1", date="20260801")
    older = _race("OLD", date="20260701")
    previous = _race("PREV", date="20260731")
    monkeypatch.setitem(api._STATE, "daily", {"races": [race]})
    legacy_without_id = {key: value for key, value in older.items() if key != "race_id"}
    monkeypatch.setitem(api._STATE, "backtest_matrix",
                        {"races": [older, legacy_without_id], "columns": []})
    monkeypatch.setitem(api._STATE, "_context_trend_cache", {})
    monkeypatch.setattr(api, "_refresh_current_daily", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_daily_for_date", lambda date: {
        "date": date, "races": [previous], "columns": [],
    })
    captured = {}
    def fake_analyze(source, _race):
        captured["source"] = source
        return {"available": False}
    monkeypatch.setattr(api.context_trends, "analyze", fake_analyze)
    got, status = api.handle_context_trends("R1")
    assert status == 200
    assert len(captured["source"]["races"]) == 3
    assert {item.get("race_id") for item in captured["source"]["races"]
            if item.get("race_id")} == {"OLD", "PREV"}


def test_user_leaderboard_handler_passes_all_shared_user_names(monkeypatch):
    race = _race("R1")
    monkeypatch.setitem(api._STATE, "daily", {"date": "20260801", "races": [race]})
    monkeypatch.setattr(api, "_refresh_current_daily", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_AUTH_ENABLED", True)

    class FakeAuth:
        @staticmethod
        def list_users():
            return [{"id": "u1", "display_name": "利用者A"}]

    monkeypatch.setattr(api, "_AUTH", FakeAuth())
    captured = {}

    def fake_board(date, races, names):
        captured.update(date=date, races=races, names=names)
        return {"date": date, "entries": []}

    monkeypatch.setattr(api.purchases, "user_leaderboard", fake_board)
    got, status = api.handle_user_leaderboard()
    assert status == 200 and got["entries"] == []
    assert captured["date"] == "20260801" and captured["races"] == [race]
    assert captured["names"]["u1"] == "利用者A"
    assert captured["names"][api.ADMIN_OWNER_ID] == "管理者"


def test_race_date_catalog_ignores_empty_days_and_links_previous(monkeypatch):
    monkeypatch.setitem(api._STATE, "date", "20260814")
    monkeypatch.setattr(api, "_built_dates",
                        lambda: ["20260808", "20260809", "20260813", "20260814"])
    race = _race("R1")
    def daily_for_date(date):
        return {
        "date": date,
        "races": [dict(race, date=date)] if date in {"20260808", "20260809"} else [],
        }
    monkeypatch.setattr(api, "_daily_for_date", daily_for_date)
    monkeypatch.setattr(api.md, "load_historical_daily",
                        lambda date, _specs: daily_for_date(date))
    monkeypatch.setattr(api.md, "today_status", lambda daily: daily["races"])
    got = api._race_date_catalog()
    assert [item["date"] for item in got["dates"]] == ["20260808", "20260809"]
    assert got["previous"]["date"] == "20260809"
    assert got["next"] is None


def test_performance_ranges_use_purchase_dates_and_previous_race_day(monkeypatch):
    monkeypatch.setitem(api._STATE, "date", "20260814")
    monkeypatch.setattr(api.purchases, "recorded_dates",
                        lambda *_args, **_kwargs: ["20260801", "20260808", "20260809"])
    monkeypatch.setattr(api, "_race_date_catalog", lambda _today=None: {
        "previous": {"date": "20260809"}, "next": None, "dates": [],
    })
    assert api._performance_dates("previous") == ["20260809"]
    assert api._performance_dates("7d") == ["20260808", "20260809"]
    assert api._performance_dates("all") == ["20260801", "20260808", "20260809"]


def test_historical_finished_race_can_be_opened_by_date(monkeypatch):
    old_race = _race("2026073101010101", date="20260731", with_order=True)
    monkeypatch.setitem(api._STATE, "date", "20260801")
    monkeypatch.setitem(api._STATE, "daily", {"date": "20260801", "races": []})
    monkeypatch.setitem(api._STATE, "preset", PRESET)
    monkeypatch.setitem(api._STATE, "specs", [])
    monkeypatch.setitem(api._STATE, "_historical_daily_cache", {})
    monkeypatch.setattr(api, "_refresh_current_daily", lambda **_kwargs: None)
    monkeypatch.setattr(api.md, "load_historical_daily", lambda date, _specs: {
        "date": date, "races": [old_race], "columns": [],
    })
    monkeypatch.setattr(api.md, "refresh_live", lambda daily, **_kwargs: daily)
    got, status = api.handle_predict({
        "race_id": old_race["race_id"], "date": "20260731", "result_only": True,
    })
    assert status == 200
    assert got["finished"] is True and got["result_only"] is True


# ---------------------------------------------------------------------------
# ライブ更新の間隔 (常駐プロセスが変わらないものを確かめ続けないこと)
# ---------------------------------------------------------------------------
def test_refresh_interval_backs_off_once_everything_is_settled(monkeypatch):
    """日付を固定した過去日で全レース確定後は間隔を伸ばすこと。

    固定 25 秒のまま回し続けたため、9日前の開催日を指定した常駐プロセスが
    4日間で累計 2.1 TB を読む事態になった。払戻訂正が後から届くことはあるので
    止めはしないが、変わらないものを確かめ続ける必要もない。
    """
    monkeypatch.setitem(api._STATE, "follow_today", False)
    settled = {"races": [{"finished": True, "payouts": [{"type": "tan"}]},
                         {"finished": True, "payouts": [{"type": "tan"}]}]}
    assert api._refresh_interval(settled) == api._IDLE_REFRESH_INTERVAL_SECONDS
    assert api._IDLE_REFRESH_INTERVAL_SECONDS > api._LIVE_REFRESH_INTERVAL_SECONDS


def test_refresh_interval_stays_short_while_anything_can_change(monkeypatch):
    monkeypatch.setitem(api._STATE, "follow_today", False)
    for daily in (
        {"races": [{"finished": True, "payouts": [{"type": "tan"}]},
                   {"finished": False}]},                     # 未発走が残っている
        {"races": [{"finished": True, "payouts": []}]},       # 払戻がまだ取れていない
        {"races": []},                                        # 開催が読めていない
    ):
        assert api._refresh_interval(daily) == api._LIVE_REFRESH_INTERVAL_SECONDS, daily


def test_refresh_interval_ignores_the_backoff_while_following_today(monkeypatch):
    """当日を追う起動では日付が変わりうるので短いままにすること。"""
    monkeypatch.setitem(api._STATE, "follow_today", True)
    settled = {"races": [{"finished": True, "payouts": [{"type": "tan"}]}]}
    assert api._refresh_interval(settled) == api._LIVE_REFRESH_INTERVAL_SECONDS
