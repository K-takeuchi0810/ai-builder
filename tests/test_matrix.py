"""matrix (特徴量行列の事前計算 + 行列上評価) のテスト。

純粋部分 (score_race_columns / evaluate_matrix) は CIセーフ。build_matrix の実DB経路は
lightgbm + keiba.db のある環境でのみ実行 (skip gate)。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config, matrix, model   # noqa: E402


_COLS = [
    {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
     "label": "平均着順", "lookback": 5, "match": []},
    {"id": "b", "key": "jockey_win_rate", "kind": "compute", "hib": True,
     "label": "騎手勝率", "lookback": None, "match": []},
]


def _hrows():
    return [
        {"num": "1", "order": 1, "x": {"a": 2.0, "b": 0.30}},
        {"num": "2", "order": 5, "x": {"a": 6.0, "b": 0.10}},
        {"num": "3", "order": 3, "x": {"a": 4.0, "b": 0.20}},
    ]


def test_score_race_columns_direction():
    # a=平均着順(小さいほど良い) → 馬1が上位
    r = matrix.score_race_columns(_hrows(), _COLS, {"a": 1.0})
    assert r[0][0] == "1" and r[-1][0] == "2"
    # b=騎手勝率(大きいほど良い) → 馬1が上位
    r2 = matrix.score_race_columns(_hrows(), _COLS, {"b": 1.0})
    assert r2[0][0] == "1"
    # 重み0は無視、未知idは無視
    r3 = matrix.score_race_columns(_hrows(), _COLS, {"a": 0.0, "zzz": 9.0})
    assert all(s == 0.0 for _, s in r3)


def test_evaluate_matrix_split_and_metrics():
    m = {"columns": _COLS, "races": [
        {"date": "20240101", "seg": {}, "horses": _hrows(),
         "tan": {"1": 250}, "trusted": True},                       # pick=1(1着) 勝ち, ret2.5
        {"date": "20250101", "seg": {}, "horses": [
            {"num": "1", "order": 4, "x": {"a": 6.0, "b": 0.1}},
            {"num": "2", "order": 2, "x": {"a": 2.0, "b": 0.3}},    # pick=2(2着) 複勝, tan無し
        ], "tan": {}, "trusted": True},
    ]}
    res = matrix.evaluate_matrix(m, {"a": 1.0}, split_date="20250101")
    assert res["overall"]["n"] == 2
    assert res["train"]["n"] == 1 and res["holdout"]["n"] == 1
    assert res["train"]["win_rate"] == 1.0            # 20240101: 馬1が1着
    assert res["holdout"]["win_rate"] == 0.0          # 20250101: 馬2は2着
    assert res["holdout"]["top3_rate"] == 1.0
    assert res["train"]["roi"] == 2.5                 # 250円/100
    # trusted=False なら回収率は None (ret_n=0)
    m["races"][0]["trusted"] = False
    res2 = matrix.evaluate_matrix(m, {"a": 1.0}, split_date="20250101")
    assert res2["train"]["roi"] is None


def test_softmax_props():
    p = matrix._softmax([("a", 1.0), ("b", -1.0)], 1.0)
    assert abs(sum(p.values()) - 1.0) < 1e-9
    assert p["a"] > p["b"]                       # 高スコアほど高確率


def test_evaluate_value_matrix_ev_selection():
    cols = [{"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
             "label": "平均着順", "lookback": 5, "match": []}]
    # 2頭: 馬1(平均着順2=良, オッズ3.0, 1着), 馬2(平均着順6, オッズ10.0, 2着)
    m = {"columns": cols, "races": [
        {"date": "20240101", "seg": {}, "trusted": True, "tan": {"1": 300}, "horses": [
            {"num": "1", "order": 1, "odds": 3.0, "x": {"a": 2.0}},
            {"num": "2", "order": 2, "odds": 10.0, "x": {"a": 6.0}},
        ]},
    ]}
    # softmax(t=1) で p1≈0.881 → EV1≈2.64, EV2≈1.19。閾値2.0 なら馬1のみ購入。
    res = matrix.evaluate_value_matrix(m, {"a": 1.0}, split_date="20250101",
                                       temperature=1.0, ev_threshold=2.0)
    assert res["train"]["bets"] == 1
    assert res["train"]["hit_rate"] == 1.0
    assert res["train"]["roi"] == 3.0            # 300円払戻 / 100円賭け
    assert res["overall"]["races_with_bet"] == 1
    # trusted=False は購入対象外
    m["races"][0]["trusted"] = False
    res2 = matrix.evaluate_value_matrix(m, {"a": 1.0}, split_date="20250101",
                                        temperature=1.0, ev_threshold=2.0)
    assert res2["overall"]["bets"] == 0


def test_prepared_matches_direct_evaluation():
    """高速経路(prepared)が低速経路(evaluate_value_matrix)と同一結果を出すこと。"""
    cols = [
        {"id": "a", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
         "label": "平均着順", "lookback": 5, "match": []},
        {"id": "b", "key": "popularity", "kind": "current", "hib": False,
         "label": "人気", "lookback": None, "match": []},
    ]
    import random
    rng = random.Random(3)
    races = []
    for yr in ("2024", "2025"):
        for _ in range(40):
            n = 6
            hs = []
            win = rng.randrange(n)
            for k in range(n):
                hs.append({"num": str(k + 1), "order": (1 if k == win else k + 2),
                           "odds": round(1.5 + k * 1.2 + rng.random(), 1), "pop": k + 1,
                           "x": {"a": float(rng.randint(1, 10)), "b": float(k + 1)}})
            races.append({"date": f"{yr}0601", "seg": {}, "trusted": True,
                          "tan": {hs[win]["num"]: int(hs[win]["odds"] * 100)}, "horses": hs})
    m = {"columns": cols, "races": races}
    weights, t, ev = {"a": 1.0, "b": -0.5}, 1.0, 1.1

    direct = matrix.evaluate_value_matrix(m, weights, "99999999", t, ev)["train"]
    prep = matrix.prepare_races(m)
    fast = matrix.value_stats_prepared(prep, weights, t, ev)
    assert direct["bets"] == fast["bets"]
    assert direct["roi"] == fast["roi"]
    assert direct["hit_rate"] == fast["hit_rate"]


def test_col_id_stable_for_aggregate_variants():
    a5 = matrix._col_id({"key": "agg_top3_rate", "lookback": 5, "match": ["surface"]})
    a3 = matrix._col_id({"key": "agg_top3_rate", "lookback": 3, "match": ["surface"]})
    assert a5 != a3                                   # lookback 違いは別カラム
    assert matrix._col_id({"key": "jockey_win_rate"}) == "jockey_win_rate"


# ---------------------------------------------------------------------------
# 実DB依存: build_matrix のスモーク
# ---------------------------------------------------------------------------
def _real_env_ready() -> bool:
    if not config.KEIBA_DB_PATH.exists():
        return False
    model._ensure_keiba_on_path()
    return importlib.util.find_spec("lightgbm") is not None


@pytest.mark.skipif(not _real_env_ready(), reason="keiba-yosou (popularity_config) が無い環境ではスキップ")
def test_build_matrix_monthly_checkpoint(tmp_path, monkeypatch):
    """月次チェックポイント・再開・範囲クリップを検証 (DB compute はモックで高速化)。"""
    import contextlib
    monkeypatch.setattr(matrix, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(matrix, "open_conn", lambda: contextlib.nullcontext(None))

    calls = []

    def fake_range(conn, lo, hi, cols, nc, npast, ma):
        calls.append((lo, hi))
        return [{"date": lo, "seg": {}, "trusted": True, "tan": {"1": 200},
                 "horses": [{"num": "1", "order": 1, "odds": 2.0, "pop": 1,
                             "x": {c["id"]: 1.0 for c in cols}}]}]

    monkeypatch.setattr(matrix, "_races_for_range", fake_range)
    specs = [{"key": "popularity"}]

    # 2 か月ぶん構築 → Jan, Feb の 2 回だけ _races_for_range が呼ばれる
    m = matrix.build_matrix("20240101", "20240228", specs)
    assert calls == [("20240101", "20240131"), ("20240201", "20240231")]
    assert len(m["races"]) == 2
    monthfiles = list((tmp_path / "months").glob("m_*.json"))
    assert len(monthfiles) == 2                       # 月ごとにチェックポイント保存

    # 範囲キャッシュを消し、月キャッシュから再開 → 追加の compute 呼び出し無し
    for f in tmp_path.glob("matrix_*.json"):
        f.unlink()
    calls.clear()
    m2 = matrix.build_matrix("20240101", "20240228", specs)
    assert calls == []                                # 全月キャッシュ済み → 再計算なし
    assert len(m2["races"]) == 2

    # 範囲クリップ: 1 月だけ要求すると Jan の 1 レースのみ (月キャッシュ再利用)
    calls.clear()
    m3 = matrix.build_matrix("20240101", "20240131", specs)
    assert calls == []
    assert [r["date"] for r in m3["races"]] == ["20240101"]



# ---------------------------------------------------------------------------
# キャッシュの再利用の条件 (2026-10-16: 途中・未来の月を空のまま永久に使っていた)
# ---------------------------------------------------------------------------
def _fake_env(tmp_path, monkeypatch, today):
    import contextlib
    monkeypatch.setattr(matrix, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(matrix, "open_conn", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(matrix, "_today_jst", lambda: today[0])
    monkeypatch.setattr(matrix, "_ensure_keiba_on_path", lambda: None)
    import sys
    import types
    fake_bt = types.ModuleType("scripts.backtest")
    fake_bt.popularity_config = lambda: {"max_snapshot_age_min": None}
    monkeypatch.setitem(sys.modules, "scripts.backtest", fake_bt)
    calls = []
    races_by_month = {}

    def fake_range(conn, lo, hi, cols, nc, npast, ma):
        calls.append(lo[:6])
        return [{"date": d, "seg": {}, "trusted": True, "tan": {}, "horses": []}
                for d in races_by_month.get(lo[:6], [])]

    monkeypatch.setattr(matrix, "_races_for_range", fake_range)
    return calls, races_by_month


def test_incomplete_month_is_rebuilt_on_a_later_day(tmp_path, monkeypatch):
    """途中の月 (今月) は作った当日だけ再利用し、翌日以降は作り直す。過ぎた月は再利用する。"""
    today = ["20261016"]
    calls, races = _fake_env(tmp_path, monkeypatch, today)
    races.update({"202609": ["20260920"], "202610": ["20261011"]})
    specs = [{"key": "popularity"}]

    m = matrix.build_matrix("20260901", "20261016", specs)
    assert calls == ["202609", "202610"] and len(m["races"]) == 2
    calls.clear()
    matrix.build_matrix("20260901", "20261016", specs)          # 同じ日: 範囲のキャッシュをそのまま使う
    assert calls == []

    today[0] = "20261018"                                       # 2 日後: 10 月に新しいレース
    races["202610"] = ["20261011", "20261017"]
    calls.clear()
    m2 = matrix.build_matrix("20260901", "20261018", specs)
    assert calls == ["202610"]                                  # 9 月 (完了) は再利用、10 月 (途中) だけ作り直す
    assert [r["date"] for r in m2["races"]] == ["20260920", "20261011", "20261017"]


def test_legacy_cache_built_before_the_month_ended_is_rebuilt(tmp_path, monkeypatch):
    """印の無い古い月のキャッシュは、更新日が月末より後なら使い、月末以前 (空の未来の月) なら作り直す。"""
    import os
    import time
    today = ["20261016"]
    calls, races = _fake_env(tmp_path, monkeypatch, today)
    races.update({"202606": ["20260607"], "202608": ["20260802"]})
    specs = [{"key": "popularity"}]
    chash = matrix._col_hash(matrix._columns(specs))
    months = tmp_path / "months"
    months.mkdir()
    old_jun = months / f"m_202606_{chash}.json"
    old_jun.write_text(json.dumps({"month": "202606", "races": [
        {"date": "20260607", "seg": {}, "trusted": True, "tan": {}, "horses": []}]}), encoding="utf-8")
    empty_aug = months / f"m_202608_{chash}.json"
    empty_aug.write_text(json.dumps({"month": "202608", "races": []}), encoding="utf-8")
    jul26 = time.mktime((2026, 7, 26, 12, 0, 0, 0, 0, -1))      # どちらも 2026-07-26 に作った
    os.utime(old_jun, (jul26, jul26))
    os.utime(empty_aug, (jul26, jul26))

    m = matrix.build_matrix("20260601", "20260831", specs)
    assert calls == ["202607", "202608"]                        # 6 月は月末後に作ったので再利用
    assert [r["date"] for r in m["races"]] == ["20260607", "20260802"]
    saved = json.loads(empty_aug.read_text(encoding="utf-8"))
    assert saved["complete"] is True and saved["built_on"] == "20261016"


def test_cache_usable_rules():
    p = None
    assert matrix._cache_usable(p, {"complete": True, "built_on": "20260101"}, "20251231", "20261016")
    assert matrix._cache_usable(p, {"complete": False, "built_on": "20261016"}, "20261031", "20261016")
    assert not matrix._cache_usable(p, {"complete": False, "built_on": "20261015"}, "20261031", "20261016")
    assert matrix._stamp("20261031", "20261016") == {"complete": False, "built_on": "20261016"}
    assert matrix._stamp("20260930", "20261016") == {"complete": True, "built_on": "20261016"}
    assert matrix._month_end("202602") == "20260228" and matrix._month_end("202402") == "20240229"
