"""プリセット重み学習のテスト。

**本丸は train-serve 一貫性**: 学習経路が normalize.py の同一実装 (欠損ポリシー) を通ること。
学習時とサービング時でゲート挙動がずれるのが最悪のスキューなので、ここをテストで固定する
(既存の 3 経路一致テストに対する 4 経路目)。
"""

from __future__ import annotations

import os
import random
import sys

import pytest

pytest.importorskip("numpy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import matrix, model, normalize as nrm, presets   # noqa: E402

COLS = [
    {"id": "form", "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
     "label": "平均着順", "lookback": 5, "match": []},
    {"id": "prize", "key": "agg_prize", "kind": "aggregate", "hib": True,
     "label": "獲得本賞金", "lookback": 5, "match": []},
]


FIELD = 10


def _matrix(n_races=120, prize_horses: int | None = None, seed=5) -> dict:
    """form は常に全馬充填。prize は各レースで先頭 prize_horses 頭だけ充填 (決定的)。

    prize_horses=None なら全馬。2 を渡すとカバレッジゲート (最低4頭・半数以上) を
    **決定的に**下回るので、テストがランダム性に左右されない。
    form が着順と相関するように勝ち馬を作るので、学習は form に正の重みを付けるはず。
    """
    rng = random.Random(seed)
    n_prize = FIELD if prize_horses is None else prize_horses
    races = []
    for i in range(n_races):
        forms = rng.sample(range(1, 15), FIELD)
        win = min(range(FIELD), key=lambda k: forms[k])    # form 最小 (最良) が勝つ
        horses = []
        for k in range(FIELD):
            x = {"form": float(forms[k])}
            if k < n_prize:
                x["prize"] = float(rng.randint(1, 50)) * 100000
            horses.append({"num": str(k + 1), "order": (1 if k == win else k + 2),
                           "odds": 2.0 + k, "pop": k + 1, "x": x})
        yr = "2021" if i < n_races // 2 else "2025"
        races.append({"date": f"{yr}0601", "seg": {}, "trusted": True,
                      "tan": {horses[win]["num"]: 300}, "horses": horses})
    return {"columns": COLS, "races": races}


# ---------------------------------------------------------------------------
# 本丸: train-serve 一貫性
# ---------------------------------------------------------------------------
def test_training_path_uses_the_same_missing_data_policy(monkeypatch):
    """学習経路が normalize.race_z を実際に呼ぶこと (ゲート実装の共有を固定)。"""
    calls = []
    real = nrm.race_z

    def spy(values, hib, n_runners=None, **kw):
        calls.append(len(values))
        return real(values, hib, n_runners, **kw)

    monkeypatch.setattr(nrm, "race_z", spy)
    monkeypatch.setattr(matrix, "nrm", nrm)          # matrix 側の参照も同じモジュール
    presets.fit_presets(_matrix(n_races=6), train_from="20210101", train_to="20251231",
                        iters=2, min_races_per_column=1)
    assert calls, "学習経路が normalize.race_z を通っていない (train-serve スキューの温床)"


def test_gate_behaviour_identical_between_training_and_serving():
    """同じレースに対し、学習側とサービング側で「使う列」が一致すること。"""
    m = _matrix(n_races=1, prize_horses=2, seed=9)   # prize は 2 割しか充填 → ゲート落ち
    prep = matrix.prepare_races(m)
    train_cols = set()
    for zc in prep[0]["z"].values():
        train_cols.update(zc.keys())

    # サービング側 (model.score_race_detailed) に同じ生値を渡す
    rows = {h["num"]: {"agg_avg_finish": h["x"].get("form"),
                       "agg_prize": h["x"].get("prize")}
            for h in m["races"][0]["horses"]}
    cfg = {"features": [{"key": "agg_avg_finish", "weight": 1.0},
                        {"key": "agg_prize", "weight": 1.0}]}
    served = model.score_race_detailed(rows, cfg)
    serve_used = {c["key"] for c in served["columns"] if c["decision"] == nrm.USE}

    # 学習側で使われた列 = サービング側で使われた列 (id と key の対応で比較)
    id2key = {"form": "agg_avg_finish", "prize": "agg_prize"}
    assert {id2key[c] for c in train_cols} == serve_used


# ---------------------------------------------------------------------------
# 列別の有効サンプル数レポート / 警告
# ---------------------------------------------------------------------------
def test_column_sample_report_counts_gate_passing_races():
    m = _matrix(n_races=40)
    prep = matrix.prepare_races(m)
    rep = presets.column_sample_report(prep, ["form", "prize"])
    assert rep["form"] == 40 and rep["prize"] == 40

    # prize のカバレッジをゲート未満にすると通過レース数が 0 になる
    m2 = _matrix(n_races=40, prize_horses=2, seed=3)
    rep2 = presets.column_sample_report(matrix.prepare_races(m2), ["form", "prize"])
    assert rep2["form"] == 40
    assert rep2["prize"] == 0


def test_low_sample_columns_are_warned():
    m = _matrix(n_races=30, prize_horses=2, seed=4)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231",
                              min_races_per_column=10, iters=5)
    warned = {w["column"] for w in res["low_sample_columns"]}
    assert "prize" in warned                       # 通過0 → 警告に出る
    assert "form" not in warned
    # ゲートを通らない列は勾配に寄与しないので重みは 0 のまま
    assert res["weights"]["prize"] == 0.0


def test_gate_check_lists_missing_columns_per_race():
    """デモ当日の事前検査: 列がゲートを通らないレースを列挙できる。"""
    m = _matrix(n_races=3, prize_horses=2, seed=7)
    checks = presets.gate_check(matrix.prepare_races(m), ["form", "prize"])
    assert checks and all("prize" in c["missing_columns"] for c in checks)
    # 全列充填なら警告なし
    ok = presets.gate_check(matrix.prepare_races(_matrix(n_races=3)), ["form", "prize"])
    assert ok == []


# ---------------------------------------------------------------------------
# 学習そのもの
# ---------------------------------------------------------------------------
def test_fit_learns_correct_sign_and_improves_likelihood():
    m = _matrix(n_races=150)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231",
                              iters=150, lr=0.5, min_races_per_column=1)
    # 平均着順は小さいほど良い (hib=False) → z は反転済みなので重みは正になるべき
    assert res["weights"]["form"] > 0
    # 学習後の対数尤度が、全ゼロ重み (一様確率) より良いこと
    prep = matrix.prepare_races(m)
    ll_zero = presets.log_likelihood(prep, ["form", "prize"], {"form": 0.0, "prize": 0.0})
    assert res["mean_log_likelihood_train"] > ll_zero


def test_train_period_is_respected_and_recorded():
    m = _matrix(n_races=100)          # 前半 2021 / 後半 2025
    res = presets.fit_presets(m, train_from="20210101", train_to="20211231", iters=3,
                              min_races_per_column=1)
    assert res["train_period"] == ["20210101", "20211231"]
    assert res["n_races_trained"] == 50          # 2021 分のみ
    # 表示期間 (学習に使わない期間) が記録されている
    assert res["display_backtest_from"]
    assert res["gate"]["min_horses"] == nrm.MIN_HORSES


def test_save_and_load_presets(tmp_path):
    m = _matrix(n_races=20)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231", iters=3,
                              min_races_per_column=1)
    p = presets.save_presets(res, tmp_path / "w.json")
    assert presets.load_presets(p)["weights"] == res["weights"]
    assert presets.load_presets(tmp_path / "none.json") == {}
