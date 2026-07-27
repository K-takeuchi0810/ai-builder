"""きょうの順位 (リーダーボード) のテスト。UI指示書 §5 の順位規則を固定する。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import leaderboard as lb   # noqa: E402

# 列ID は設定から導出される本物の ID を使う (configs.selected_columns と一致させる)。
COL_FORM = "agg_avg_finish|lb=5|m="      # 平均着順 (小さいほど良い)
COL_PRIZE = "agg_prize|lb=5|m="          # 獲得本賞金 (大きいほど良い)
PRESET = {"weights": {COL_FORM: 1.0, COL_PRIZE: 1.0}}
COLS = [
    {"id": COL_FORM, "key": "agg_avg_finish", "kind": "aggregate", "hib": False,
     "label": "平均着順", "lookback": 5, "match": []},
    {"id": COL_PRIZE, "key": "agg_prize", "kind": "aggregate", "hib": True,
     "label": "獲得本賞金", "lookback": 5, "match": []},
]

# 参加者AI 2種: A は「平均着順が小さい馬」、B は「本賞金が大きい馬」を◎にする
CFG_A = {"name": "AI-A", "step1": [],
         "step2": [{"metric": "agg_avg_finish", "match": [], "lookback": 5}]}
CFG_B = {"name": "AI-B", "step1": [],
         "step2": [{"metric": "agg_prize", "match": [], "lookback": 5}]}


def _race(rid, *, winner: str, favorite: str = "1", n=6, finished=True):
    """馬番1が常に1番人気。winner の馬が1着になるレースを作る。"""
    horses = []
    for k in range(1, n + 1):
        num = str(k)
        order = 1 if num == winner else (2 if num != winner and k == 1 else k + 1)
        horses.append({
            "num": num, "name": f"馬{k}",
            "order": (order if finished else 0),
            "pop": 1 if num == favorite else k + 1,
            "odds": 2.0 + k,
            # A の指標: winner が最小 / B の指標: 別の馬 (6番) が最大
            "x": {COL_FORM: 1.0 if num == winner else 5.0 + k,
                  COL_PRIZE: 9_000_000.0 if num == "6" else 1_000_000.0},
        })
    return {"race_id": rid, "date": "20260801", "seg": {}, "trusted": True,
            "tan": {winner: 300}, "horses": horses}


def _daily(races):
    return {"date": "20260801", "columns": COLS, "races": races}


def _configs():
    return [{"id": "a", "name": "AI-A", "config": CFG_A},
            {"id": "b", "name": "AI-B", "config": CFG_B}]


def test_win_hits_and_upset_hits():
    """◎が1番人気と違い、かつ的中したら「出し抜き」に数える。"""
    # 3レースとも馬番3が勝つ (1番人気は馬番1) → A は毎回◎3番で的中=出し抜き
    daily = _daily([_race(f"R{i}", winner="3") for i in range(3)])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    by = {e["name"]: e for e in got["entries"]}

    assert by["AI-A"]["win_hits"] == 3
    assert by["AI-A"]["upset_hits"] == 3          # ◎(3番) ≠ 1番人気(1番)
    # B は常に6番を◎にするので的中0
    assert by["AI-B"]["win_hits"] == 0
    assert by["AI-B"]["upset_hits"] == 0
    # ベースラインは1番人気(1番)を◎にするので的中0
    assert by["1番人気AI"]["win_hits"] == 0
    assert by["1番人気AI"]["is_baseline"] is True


def test_favorite_win_is_not_an_upset():
    """1番人気が勝ったとき、◎=1番人気なら出し抜きにはならない。"""
    daily = _daily([_race("R1", winner="1")])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    by = {e["name"]: e for e in got["entries"]}
    assert by["1番人気AI"]["win_hits"] == 1
    assert by["1番人気AI"]["upset_hits"] == 0     # 人気どおりなので出し抜きではない


def test_ranking_order_and_baseline_has_no_rank():
    daily = _daily([_race(f"R{i}", winner="3") for i in range(3)])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    entries = got["entries"]

    # ①◎的中率の降順 → A が先頭
    assert entries[0]["name"] == "AI-A" and entries[0]["rank"] == 1
    # ベースラインには順位数字を付けない (参加者と競わせない)
    base = next(e for e in entries if e["is_baseline"])
    assert base["rank"] is None
    # 参加者AIには必ず順位が付く
    assert all(e["rank"] is not None for e in entries if not e["is_baseline"])


def test_ties_share_the_same_rank():
    """①②③すべて同点なら同順位で表示する。"""
    daily = _daily([_race("R1", winner="3")])
    same = [{"id": "a", "name": "AI-A", "config": CFG_A},
            {"id": "a2", "name": "AI-A2", "config": CFG_A}]   # 同一設定 = 同成績
    got = lb.build_leaderboard(daily, PRESET, configs=same)
    ranks = {e["name"]: e["rank"] for e in got["entries"] if not e["is_baseline"]}
    assert ranks["AI-A"] == ranks["AI-A2"] == 1


def test_unfinished_races_are_excluded():
    daily = _daily([_race("R1", winner="3", finished=False)])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    assert got["n_races_finished"] == 0
    by = {e["name"]: e for e in got["entries"]}
    assert by["AI-A"]["races"] == 0
    assert by["AI-A"]["show_rate"] is None        # 0% と誤読させない


def test_show_rate_and_no_roi_key():
    daily = _daily([_race(f"R{i}", winner="3") for i in range(4)])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    by = {e["name"]: e for e in got["entries"]}
    assert by["AI-A"]["show_rate"] == 1.0        # ◎が毎回1着なら複勝率100%
    # 回収率は 2026-07-27 の判断で表示に変更。ただし **不確かさが必ず付く**
    for e in got["entries"]:
        st = e["roi_stats"]
        assert set(("races", "enough", "roi", "ci", "top_share",
                    "long_run_ceiling", "min_races")) <= set(st)
        # 4レースしかないので数値を出さない判定
        assert st["enough"] is False and st["ci"] is None
        assert e["roi_rank"] is None
    # 判断C: 順位規則を画面に出せるよう、4段すべてを文章で返す
    rule = got["ranking_rule"]
    for part in ("◎的中率", "出し抜", "複勝率", "同順位"):
        assert part in rule, part
    # 対抗戦・煽り系の語彙を混ぜない (設計書 v0.3 §1 DON'T)
    for banned in ("対抗戦", "勝負", "優勝"):
        assert banned not in rule


# ---------------------------------------------------------------------------
# 回収率ランキング (2026-07-27 の判断で追加)
# ---------------------------------------------------------------------------
def test_roi_needs_enough_races_before_a_number_appears():
    """最小レース数に届くまで回収率の数値と順位を出さないこと。"""
    from builder import roi
    daily = _daily([_race(f"R{i}", winner="3") for i in range(4)])
    got = lb.build_leaderboard(daily, PRESET, configs=_configs())
    for e in got["entries"]:
        assert e["roi_stats"]["races"] <= 4
        assert e["roi_stats"]["enough"] is False
        assert e["roi_rank"] is None
    assert got["roi_min_races"] == roi.MIN_RACES_FOR_ROI
    assert "控除率" in got["roi_note"] and "誤差" not in got["roi_note"]
    assert got["roi_long_run_ceiling"] == 0.8


def test_roi_ranking_treats_overlapping_intervals_as_tied():
    """信頼区間が重なる相手は同順位にすること。

    1日36レースでは回収率は「◎に30倍が来たか」でほぼ決まる。区間が重なって
    いるのに順位を確定させると、運の差を実力の差として見せることになる。
    """
    from builder import roi
    wide_a = {"enough": True, "roi": 1.20, "ci": [0.30, 2.10]}
    wide_b = {"enough": True, "roi": 0.90, "ci": [0.20, 1.60]}
    far = {"enough": True, "roi": 0.10, "ci": [0.02, 0.18]}
    entries = [{"name": "A", "roi_stats": wide_a},
               {"name": "B", "roi_stats": wide_b},
               {"name": "C", "roi_stats": far}]
    roi.rank(entries)
    by = {e["name"]: e for e in entries}
    assert by["A"]["roi_rank"] == 1
    assert by["B"]["roi_rank"] == 1, "区間が重なるのに順位を分けている"
    assert by["C"]["roi_rank"] == 3
    assert by["B"]["roi_tied_with_leader"] is True
    assert by["C"]["roi_tied_with_leader"] is False


def test_roi_ranking_skips_the_baseline_and_insufficient_entries():
    from builder import roi
    entries = [
        {"name": "基準", "is_baseline": True,
         "roi_stats": {"enough": True, "roi": 0.78, "ci": [0.70, 0.86]}},
        {"name": "A", "roi_stats": {"enough": True, "roi": 0.50, "ci": [0.40, 0.60]}},
        {"name": "B", "roi_stats": {"enough": False, "roi": 3.0, "ci": None}},
    ]
    roi.rank(entries)
    by = {e["name"]: e for e in entries}
    assert by["基準"]["roi_rank"] is None, "基準に順位を付けている"
    assert by["A"]["roi_rank"] == 1
    assert by["B"]["roi_rank"] is None, "レース数不足に順位を付けている"


def test_unit_returns_uses_the_win_payout():
    """単勝払戻から1単位あたりの収益列を作ること。"""
    from builder import roi
    race_hit = {"tan": {"03": 450}}
    race_miss = {"tan": {"07": 220}}
    got = roi.unit_returns([("03", race_hit), ("03", race_miss)])
    assert list(got) == [4.5, 0.0]
    # 払戻が無い (未確定) レースは 0
    assert list(roi.unit_returns([("01", {})])) == [0.0]
