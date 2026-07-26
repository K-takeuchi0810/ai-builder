"""matrix_daily (当日レースの日次バッチ) のテスト。

DB 層はモックして高速・CIセーフに保つ。実 DB 経路は skip gate 付きで1本だけ。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, matrix, matrix_daily as md, model   # noqa: E402

SPECS = [{"key": "popularity"},
         {"key": "agg_avg_finish", "lookback": 5, "match": []}]


def _fake_race(race_num="01", weight="480"):
    return {"race_year": "2026", "race_month_day": "0801", "track_code": "04",
            "kaiji": "03", "nichiji": "05", "race_num": race_num, "distance": 1800,
            "track_type_code": "11", "turf_condition": "1", "dirt_condition": "0",
            "weather_code": "1", "race_name": f"テスト{race_num}R"}


def _fake_horses(n=8, weight="480", with_order=False):
    out = []
    for k in range(1, n + 1):
        out.append({"horse_num": f"{k:02d}", "blood_register_num": f"20201000{k:02d}",
                    "horse_name": f"テストホース{k}", "win_odds": 20 + k * 10,
                    "win_popularity": k, "horse_weight": weight,
                    "confirmed_order": (k if with_order else 0)})
    return out


def _patch_db(monkeypatch, races, horses, past_runs=None):
    """keiba-yosou の DB 依存をすべてモックする。"""
    monkeypatch.setattr(md, "open_conn", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(md, "_ensure_keiba_on_path", lambda: None)

    fake = type(sys)("scripts.backtest")
    fake.list_races = lambda conn, lo, hi, jra_only=True, require_confirmed=False: races
    fake.horses_for_race = lambda conn, race: horses
    fake.get_payout_row = lambda conn, race: None
    fake.popularity_config = lambda: {"max_snapshot_age_min": 30}
    fake.race_odds_untrusted = lambda h, r, a: False
    scripts = type(sys)("scripts")
    scripts.backtest = fake
    monkeypatch.setitem(sys.modules, "scripts", scripts)
    monkeypatch.setitem(sys.modules, "scripts.backtest", fake)
    monkeypatch.setattr(model, "_past_runs",
                        lambda conn, brn, before, limit=50, cache=None: past_runs or [])


def test_build_daily_uses_entry_list_without_results(tmp_path, monkeypatch):
    """当日は結果が無い (confirmed_order=0) 状態でも基底列を計算できること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))

    daily = md.build_daily("20260801", SPECS)
    assert daily["date"] == "20260801"
    assert len(daily["races"]) == 1
    r = daily["races"][0]
    assert r["race_id"] == "202608010403050 1".replace(" ", "")   # 年月日場回日R
    assert len(r["horses"]) == 8
    h = r["horses"][0]
    assert h["order"] == 0                        # 結果はまだ無い
    assert h["odds"] == 3.0                       # win_odds 30 → 3.0倍
    assert h["name"] == "テストホース1"
    assert h["n_past_runs"] == 0                   # カバレッジ表示用
    assert set(h["x"]) == {c["id"] for c in daily["columns"]}
    assert r["weight_announced"] is True           # horse_weight が数字


def test_race_name_falls_back_to_conditions(tmp_path, monkeypatch):
    """平場は race_name が空 (重賞のみ命名) なので条件から表示名を作ること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    race = _fake_race()
    race["race_name"] = ""                      # 実データの平場と同じ状態
    _patch_db(monkeypatch, [race], _fake_horses(8))
    daily = md.build_daily("20260801", SPECS)
    name = daily["races"][0]["race_name"]
    assert name and name.strip()                # 空文字を UI に出さない
    assert "1800m" in name                      # 距離が入る
    assert "芝" in name                          # 芝ダートが入る

    # 重賞など名前があるレースはそのまま使う
    named = _fake_race()
    named["race_name"] = "テスト記念"
    _patch_db(monkeypatch, [named], _fake_horses(8))
    daily2 = md.build_daily("20260802", SPECS)
    assert daily2["races"][0]["race_name"] == "テスト記念"


def test_weight_not_announced_is_detected(tmp_path, monkeypatch):
    """馬体重未発表なら weight_announced=False (設計書 §3.3 の「分析待ち」判定)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8, weight="   "))
    daily = md.build_daily("20260801", SPECS)
    assert daily["races"][0]["weight_announced"] is False


def test_daily_cache_roundtrip_and_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    calls = []
    races = [_fake_race()]

    def counting_list_races(conn, lo, hi, jra_only=True, require_confirmed=False):
        calls.append(lo)
        return races

    _patch_db(monkeypatch, races, _fake_horses(6))
    sys.modules["scripts.backtest"].list_races = counting_list_races

    md.build_daily("20260801", SPECS)
    assert len(calls) == 1
    md.build_daily("20260801", SPECS)                 # キャッシュから
    assert len(calls) == 1
    assert md.load_daily("20260801", SPECS)["date"] == "20260801"
    md.build_daily("20260801", SPECS, rebuild=True)    # 強制再計算
    assert len(calls) == 2
    # 原子的書き込み: .tmp が残らない
    assert not list((tmp_path / "daily").glob("*.tmp"))


def test_find_race_and_today_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race("01"), _fake_race("02")], _fake_horses(8))
    daily = md.build_daily("20260801", SPECS)

    rid = daily["races"][0]["race_id"]
    assert md.find_race(daily, rid)["race_num"] == "01"
    assert md.find_race(daily, "nope") is None

    status = md.today_status(daily)
    assert len(status) == 2
    s = status[0]
    assert s["race_id"] == rid and s["n_horses"] == 8
    assert s["weight_announced"] is True
    # popularity は全馬充填なのでゲート通過、agg_avg_finish は過去走0件で欠損 → 未通過
    assert "agg_avg_finish|lb=5|m=" in s["gate_missing_columns"]
    assert s["n_gate_missing"] == 1 and s["n_gate_passed"] == 1
    assert s["gate_pass_rate"] == 0.5
    # 一部の列が欠けても「予想は可能」なので ready は True
    # (実測: 直近レースでも 425列中 中央値5列は欠ける。全列通過を要求するのは誤り)
    assert s["ready"] is True


def test_today_status_ready_when_all_columns_pass(tmp_path, monkeypatch):
    """全列がゲートを通れば未通過0・通過率1.0。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    daily = md.build_daily("20260801", [{"key": "popularity"}])
    status = md.today_status(daily)
    assert status[0]["gate_missing_columns"] == []
    assert status[0]["gate_pass_rate"] == 1.0
    assert status[0]["ready"] is True


def test_today_status_not_ready_when_nothing_passes(tmp_path, monkeypatch):
    """1列も通らないレースは ready=False (予想が成立しない)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    # 過去走ゼロ → 集計列のみの構成では 1 列も通らない
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(8))
    daily = md.build_daily("20260801", [{"key": "agg_avg_finish", "lookback": 5, "match": []}])
    status = md.today_status(daily)
    assert status[0]["n_gate_passed"] == 0
    assert status[0]["gate_pass_rate"] == 0.0
    assert status[0]["ready"] is False


def test_output_shape_is_compatible_with_matrix_helpers(tmp_path, monkeypatch):
    """日次出力が prepare_races / score_race_detailed でそのまま使えること。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    _patch_db(monkeypatch, [_fake_race()], _fake_horses(10))
    daily = md.build_daily("20260801", [{"key": "popularity"}])

    prep = matrix.prepare_races(daily)
    assert len(prep) == 1 and prep[0]["race_id"] == daily["races"][0]["race_id"]
    assert prep[0]["index"] == 0
    # 人気 (小さいほど良い) の z が入っている
    assert prep[0]["z"]["01"]["popularity"] > prep[0]["z"]["10"]["popularity"]


@pytest.mark.skipif(
    not config.KEIBA_DB_PATH.exists() or importlib.util.find_spec("lightgbm") is None,
    reason="実 DB / lightgbm が無い環境ではスキップ")
def test_real_db_daily_build_smoke(tmp_path, monkeypatch):
    """実 DB で確定済みの1日を日次ビルドできること (PIT: 過去走は前日まで)。"""
    monkeypatch.setattr(config, "CORNER_INDEX_PATH", tmp_path / "corner.json")
    daily = md.build_daily("20250105", SPECS, require_confirmed=True)
    if not daily["races"]:
        pytest.skip("対象日にレースが無い")
    r = daily["races"][0]
    assert r["date"] == "20250105" and r["horses"]
    for h in r["horses"]:
        assert set(h["x"]) == {c["id"] for c in daily["columns"]}
    st = md.today_status(daily)
    assert len(st) == len(daily["races"])
