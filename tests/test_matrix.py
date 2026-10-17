"""matrix (特徴量行列の事前計算 + 行列上評価) のテスト。

純粋部分 (score_race_columns / evaluate_matrix) は CIセーフ。build_matrix の実DB経路は
lightgbm + keiba.db のある環境でのみ実行 (skip gate)。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
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
    assert calls == [("20240101", "20240131"), ("20240201", "20240229")]   # 実在する月末まで
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
    st = matrix._stamp("20261031", "20261016")
    assert (st["complete"], st["built_on"]) == (False, "20261016") and st["built_at"].endswith("+09:00")
    assert matrix._stamp("20260930", "20261016")["complete"] is True
    assert matrix._month_end("202602") == "20260228" and matrix._month_end("202402") == "20240229"



def test_month_range_ends_on_a_real_day(tmp_path, monkeypatch):
    """各月は実在する月末までを読む。30 日の月に 31 日を渡すと keiba-yosou の期間の検査が止める。"""
    today = ["20261016"]
    _calls, _races = _fake_env(tmp_path, monkeypatch, today)
    seen = []

    def strict_range(conn, lo, hi, cols, nc, npast, ma):
        from datetime import datetime
        datetime.strptime(hi, "%Y%m%d")                 # 実在しない日付なら ValueError
        seen.append((lo, hi))
        return []

    monkeypatch.setattr(matrix, "_races_for_range", strict_range)
    matrix.build_matrix("20260901", "20261016", [{"key": "popularity"}])
    assert seen == [("20260901", "20260930"), ("20261001", "20261031")]



# ---------------------------------------------------------------------------
# keiba-yosou の改訂の台帳 (2026-10-18: DB の中身の修正がキャッシュに反映されなかった)
# ---------------------------------------------------------------------------
def _write_revisions(tmp_path, monkeypatch, revisions):
    dbp = tmp_path / "keiba" / "keiba.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    (dbp.parent / "data_revisions.json").write_text(json.dumps(revisions), encoding="utf-8")
    monkeypatch.setattr(matrix.config, "KEIBA_DB_PATH", dbp)


def test_revision_after_the_cache_rebuilds_affected_months_only(tmp_path, monkeypatch):
    """キャッシュを作った後の改訂は、effective_from 以降の月だけを古くする (その前の月は使い続ける)。"""
    today = ["20261016"]
    calls, races = _fake_env(tmp_path, monkeypatch, today)
    races.update({"202608": ["20260802"], "202609": ["20260920"]})
    _write_revisions(tmp_path, monkeypatch, [])
    specs = [{"key": "popularity"}]
    matrix.build_matrix("20260801", "20260930", specs)
    assert calls == ["202608", "202609"]

    _write_revisions(tmp_path, monkeypatch, [{"at": "2999-01-01T00:00:00+09:00", "kind": "backfill",
                                              "effective_from": "20260901"}])
    calls.clear()
    m = matrix.build_matrix("20260801", "20260930", specs)
    assert calls == ["202609"]                          # 8 月は改訂の範囲外なので使い回す
    assert "stale_by_revision" not in m


def test_reuse_mode_keeps_stale_months_and_reports_them(tmp_path, monkeypatch):
    """サービスの起動 (stale_data="reuse") は古い月を作り直さず、どの月かを返す。"""
    today = ["20261016"]
    calls, races = _fake_env(tmp_path, monkeypatch, today)
    races.update({"202608": ["20260802"]})
    _write_revisions(tmp_path, monkeypatch, [])
    specs = [{"key": "popularity"}]
    matrix.build_matrix("20260801", "20260831", specs)
    _write_revisions(tmp_path, monkeypatch, [{"at": "2999-01-01T00:00:00+09:00", "kind": "repair",
                                              "effective_from": "19860101"}])
    for f in tmp_path.glob("matrix_*.json"):
        f.unlink()
    calls.clear()
    m = matrix.build_matrix("20260801", "20260831", specs, stale_data="reuse")
    assert calls == [] and m["stale_by_revision"] == ["202608"]
    calls.clear()
    m2 = matrix.build_matrix("20260801", "20260831", specs, stale_data="reuse")   # 範囲のキャッシュも古い扱い
    assert calls == [] and m2.get("stale_by_revision")


def test_revision_before_the_cache_does_not_rebuild(tmp_path, monkeypatch):
    today = ["20261016"]
    calls, races = _fake_env(tmp_path, monkeypatch, today)
    _write_revisions(tmp_path, monkeypatch, [{"at": "2000-01-01T00:00:00+09:00", "kind": "old",
                                              "effective_from": "19860101"}])
    specs = [{"key": "popularity"}]
    matrix.build_matrix("20260801", "20260831", specs)
    calls.clear()
    matrix.build_matrix("20260801", "20260831", specs)
    assert calls == []


def test_revisions_after_rules_and_broken_ledger(tmp_path, monkeypatch):
    revs = [{"at": "2026-10-14T22:36:00+09:00", "effective_from": "19860101"},
            {"at": "2026-10-05T12:00:00+09:00", "effective_from": "20210101"}]
    assert len(matrix._revisions_after("2026-10-01T00:00:00+09:00", "20260831", revs)) == 2
    assert len(matrix._revisions_after("2026-10-10T00:00:00+09:00", "20260831", revs)) == 1
    assert matrix._revisions_after("2026-10-10T00:00:00+09:00", "20201231", revs[1:]) == []
    dbp = tmp_path / "k" / "keiba.db"
    dbp.parent.mkdir()
    (dbp.parent / "data_revisions.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(matrix.config, "KEIBA_DB_PATH", dbp)
    assert matrix._load_revisions() == []
    import pytest
    with pytest.raises(ValueError):
        matrix.build_matrix("20260801", "20260831", [{"key": "popularity"}], stale_data="maybe")


def test_rebuild_matrix_lists_stale_months(tmp_path, monkeypatch):
    from builder import rebuild_matrix
    today = ["20261016"]
    _calls, races = _fake_env(tmp_path, monkeypatch, today)
    _write_revisions(tmp_path, monkeypatch, [])
    specs = [{"key": "popularity"}]
    matrix.build_matrix("20260801", "20260930", specs)
    assert rebuild_matrix.stale_months("20260801", "20260930", specs) == []
    _write_revisions(tmp_path, monkeypatch, [{"at": "2999-01-01T00:00:00+09:00", "kind": "x",
                                              "effective_from": "20260901"}])
    assert rebuild_matrix.stale_months("20260801", "20261016", specs) == ["202609", "202610"]



def test_revision_times_are_compared_as_datetimes():
    """+09:00 と +00:00 が混ざっても正しく比べる (文字列の比較だと 9 時間ずれる)。"""
    revs = [{"at": "2026-10-14T13:36:00+00:00", "effective_from": "19860101"}]       # = 22:36 JST
    assert matrix._revisions_after("2026-10-14T22:00:00+09:00", "20260831", revs)      # 改訂はキャッシュより後
    assert not matrix._revisions_after("2026-10-14T23:00:00+09:00", "20260831", revs)  # 改訂はキャッシュより前
    bad = [{"at": "not-a-time", "effective_from": "19860101"}]
    assert matrix._revisions_after("2026-10-14T23:00:00+09:00", "20260831", bad) == bad   # 読めなければ古い扱い


def test_range_built_at_is_the_oldest_month(tmp_path, monkeypatch):
    """範囲のキャッシュの built_at は組み立てた月の最も古いもの (古い月を含む範囲を新しく見せない)。"""
    today = ["20261016"]
    _calls, races = _fake_env(tmp_path, monkeypatch, today)
    _write_revisions(tmp_path, monkeypatch, [])
    specs = [{"key": "popularity"}]
    m = matrix.build_matrix("20260801", "20260930", specs)
    chash = matrix._col_hash(matrix._columns(specs))
    months = [json.loads(matrix._month_cache_path(ym, chash).read_text(encoding="utf-8"))["built_at"]
              for ym in ("202608", "202609")]
    assert m["built_at"] == min(months, key=matrix._as_dt)



def test_unreadable_ledger_stops_rebuild_matrix(tmp_path, monkeypatch, capsys):
    """台帳が在るのに読めないとき、rebuild_matrix は「作り直す月: 0」で黙って終わらず終了コード 2。
    サービス側 (strict=False) は改訂なしとして続ける。"""
    from builder import rebuild_matrix
    today = ["20261016"]
    _fake_env(tmp_path, monkeypatch, today)
    _write_revisions(tmp_path, monkeypatch, [])
    path = matrix._revisions_path()
    path.write_text("{broken", encoding="utf-8")
    assert matrix._load_revisions() == []
    with pytest.raises(matrix.RevisionLedgerError):
        matrix._load_revisions(strict=True)
    monkeypatch.setattr(rebuild_matrix.specs, "maib_all_specs", lambda: [{"key": "popularity"}])
    assert rebuild_matrix.main(["--from", "20260801", "--to", "20260831"]) == 2
    assert "中止" in capsys.readouterr().out


def test_rebuild_lock_refuses_a_second_run_and_is_removed(tmp_path, monkeypatch):
    from builder import rebuild_matrix
    today = ["20261016"]
    _fake_env(tmp_path, monkeypatch, today)
    _write_revisions(tmp_path, monkeypatch, [])
    monkeypatch.setattr(rebuild_matrix.specs, "maib_all_specs", lambda: [{"key": "popularity"}])
    lock = matrix.rebuild_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("{}", encoding="utf-8")
    assert rebuild_matrix.main(["--from", "20260801", "--to", "20260831", "--apply"]) == 3
    lock.unlink()
    assert rebuild_matrix.main(["--from", "20260801", "--to", "20260831", "--apply"]) == 0
    assert not lock.exists()


def test_atomic_write_retries_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    target = tmp_path / "m.json"
    real = pathlib.Path.replace
    fails = [2]

    def flaky(self, dst):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("in use")
        return real(self, dst)
    monkeypatch.setattr(pathlib.Path, "replace", flaky)
    monkeypatch.setattr(matrix.time, "sleep", lambda s: None)
    matrix._write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
