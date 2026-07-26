"""プリセット重み学習のテスト。

**本丸は train-serve 一貫性**: 学習経路が normalize.py の同一実装 (欠損ポリシー) を通ること。
学習時とサービング時でゲート挙動がずれるのが最悪のスキューなので、ここをテストで固定する
(既存の 3 経路一致テストに対する 4 経路目)。
"""

from __future__ import annotations

import math
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


def test_fit_never_ends_worse_than_uniform_even_with_huge_lr():
    """回帰テスト: 大きすぎる学習率でも発散しないこと。

    実測で lr=0.5・128列・15,549レースの学習が発散し、平均対数尤度が一様分布
    (14頭なら約 -2.6) より遥かに悪い -13.16 になった。バックテスト再生で
    参加者に無意味な印を見せてしまうため、単調改善を保証する。
    """
    m = _matrix(n_races=120)
    prep = matrix.prepare_races(m)
    cols = ["form", "prize"]
    ll_uniform = presets.log_likelihood(prep, cols, {c: 0.0 for c in cols})

    for lr in (0.1, 1.0, 50.0):                 # 極端な学習率でも
        res = presets.fit_presets(m, train_from="20210101", train_to="20251231",
                                  iters=80, lr=lr, min_races_per_column=1)
        assert res["mean_log_likelihood_train"] >= ll_uniform, f"lr={lr} で発散"
        # 一様分布 = log(1/頭数) より良い側にいること
        assert res["mean_log_likelihood_train"] > math.log(1.0 / FIELD) - 1e-9


def test_confidence_thresholds_are_ordered():
    m = _matrix(n_races=80)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231",
                              iters=40, min_races_per_column=1)
    th = res["confidence_thresholds"]
    assert th["n"] > 0
    assert th["solid"] >= th["strong"] >= 0      # 鉄板級の閾値 ≥ 有力の閾値
    assert presets.confidence_label(th["solid"] + 1, th) == "鉄板級"
    assert presets.confidence_label(-1.0, th) == "混戦"


def test_train_period_is_respected_and_recorded():
    m = _matrix(n_races=100)          # 前半 2021 / 後半 2025
    res = presets.fit_presets(m, train_from="20210101", train_to="20211231", iters=3,
                              min_races_per_column=1)
    assert res["train_period"] == ["20210101", "20211231"]
    assert res["n_races_trained"] == 50          # 2021 分のみ
    # 表示期間 (学習に使わない期間) が記録されている
    assert res["display_backtest_from"]
    assert res["gate"]["min_horses"] == nrm.MIN_HORSES


def test_columns_fingerprint_detects_old_model():
    """デモ当日に旧モデル (別の列構成) を掴む事故を構造的に防ぐ。"""
    m = _matrix(n_races=20)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231", iters=3,
                              min_races_per_column=1)
    cols = ["form", "prize"]
    assert res["n_columns"] == 2
    assert res["columns_fingerprint"] == presets.columns_fingerprint(cols)
    # 同じ列構成なら問題なし
    assert presets.check_preset_matches_spec(res, cols) is None
    assert presets.check_preset_matches_spec(res, list(reversed(cols))) is None  # 順序無関係
    # 列構成が違えば不一致として検出
    bad = presets.check_preset_matches_spec(res, ["form", "prize", "extra"])
    assert bad["code"] == "preset_column_mismatch"
    assert bad["preset_n_columns"] == 2
    # 未学習も検出
    assert presets.check_preset_matches_spec({}, cols)["code"] == "no_preset_weights"


def test_save_and_load_presets(tmp_path):
    m = _matrix(n_races=20)
    res = presets.fit_presets(m, train_from="20210101", train_to="20251231", iters=3,
                              min_races_per_column=1)
    p = presets.save_presets(res, tmp_path / "w.json")
    assert presets.load_presets(p)["weights"] == res["weights"]
    assert presets.load_presets(tmp_path / "none.json") == {}


# ---------------------------------------------------------------------------
# 列別 (単独) 学習 — 部分集合でも尺度が揃う重みを作る経路
# ---------------------------------------------------------------------------
def test_per_column_fit_matches_joint_fit_on_a_single_column():
    """1列しかない構成では、列別学習と同時学習は同じ解に収束すること。

    列別学習は 1 次元 Newton、同時学習は勾配上昇 + バックトラッキングで実装が
    別なので、同一問題で一致することを固定して実装ミスを検出する。
    """
    prep = matrix.prepare_races(_matrix(n_races=200))
    uni = presets.fit_per_column(prep, ["form"], l2=1.0, iters=50)
    joint = presets.fit_conditional_logit(prep, ["form"], l2=1.0, iters=300)
    # hib=False の向き付けは race_z 側で済んでいるので「良い=高い z」。重みは正。
    assert uni["form"] > 0
    assert math.isclose(uni["form"], joint["form"], rel_tol=0.02), (uni, joint)


def test_per_column_fit_is_independent_of_other_columns():
    """列別学習は他列の有無で重みが変わらないこと (これが部分集合で揃う理由)。

    同時学習では列を足すと既存列の重みが動く (共変量調整) ため、参加者が選ぶ
    部分集合ごとに尺度が変わってしまう。列別学習はそれが起きない。
    """
    prep = matrix.prepare_races(_matrix(n_races=200))
    alone = presets.fit_per_column(prep, ["form"])["form"]
    with_other = presets.fit_per_column(prep, ["form", "prize"])["form"]
    assert alone == with_other

    # 対照: 同時学習は他列を足すと動く (= 部分集合で尺度がずれる)
    j_alone = presets.fit_conditional_logit(prep, ["form"])["form"]
    j_both = presets.fit_conditional_logit(prep, ["form", "prize"])["form"]
    assert j_alone != j_both


def test_per_column_fit_gives_zero_to_uninformative_columns():
    """ゲートを通らない/分散が無い列は重み0 (勾配も曲率も消える)。"""
    prep = matrix.prepare_races(_matrix(n_races=60, prize_horses=2))
    w = presets.fit_per_column(prep, ["form", "prize"])
    assert w["prize"] == 0.0          # カバレッジ不足でゲート未通過
    assert w["form"] != 0.0


def test_per_column_fit_never_worse_than_uniform_per_column():
    """各列単独で見たとき、列別重みは一様分布 (重み0) 以上であること。

    列別学習の存在意義は「単独選択でまともに効く」ことなので、そこを固定する。
    """
    prep = matrix.prepare_races(_matrix(n_races=200))
    w = presets.fit_per_column(prep, ["form", "prize"])
    for cid in ("form", "prize"):
        ll_fit = presets.log_likelihood(prep, [cid], w)
        ll_uni = presets.log_likelihood(prep, [cid], {cid: 0.0})
        assert ll_fit >= ll_uni - 1e-9, (cid, ll_fit, ll_uni)


def test_pack_preserves_race_structure():
    """_pack のレース境界と勝ち馬位置が prepare_races と一致すること。"""
    prep = matrix.prepare_races(_matrix(n_races=20))
    z, starts, wins = presets._pack(prep, ["form", "prize"])
    assert len(starts) == len(wins) + 1
    assert starts[-1] == z.shape[0] == sum(len(r["z"]) for r in prep)
    for k, r in enumerate(prep):
        nums = list(r["z"].keys())
        assert starts[k + 1] - starts[k] == len(nums)
        assert r["order"][nums[wins[k]]] == 1        # 勝ち馬を指している
