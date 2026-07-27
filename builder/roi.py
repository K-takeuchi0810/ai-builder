"""回収率 (単勝) の集計 — **不確かさを構造として付ける**。

## なぜこのモジュールが慎重なのか

設計書 v0.3 は当初「回収率をどこにも表示しない」としていた。根拠は実測:

- 182,594 候補 (35,036 + 119,069 + 63,525) を探索して、再現する OOS エッジは **0件**
- 回収率で浮上した3件は ◎ の勝率が **2.6〜8.0%** (1番人気は 33〜35%) の大穴くじで、
  信頼区間を見れば再現しないことが確認された
- 1番人気ベースラインの回収率は 0.776 / 0.774 — つまり **控除率がそのまま出る**

2026-07-27 に「ビルダーの回収率をランキングして比較したい」という判断が入り、
表示することになった。ただし **1日36レースでは回収率は「誰かの◎に30倍が来たか」で
ほぼ決まる**ため、素の点推定で順位を付けると最も運が良かった人が勝つ。

そこでこのモジュールは、点推定と一緒に必ず

1. **ブートストラップ信頼区間** (単勝配当はヘビーテールなので点推定は危険)
2. **最小レース数のゲート** (これ未満は数値を出さない)
3. **控除率の上限** (単勝の控除率は 20% なので長期の上限は約 80%)
4. **最大配当が全体をどれだけ支配しているか** (1本の万馬券で説明できるか)

を返す。UI はこれらを **必ず併記する** (テストで固定)。
"""

from __future__ import annotations

import numpy as np

from .segsearch import bootstrap_roi_ci

# 単勝の控除率は 20% (JRA)。長期の回収率はこれを上回らない。
TAKEOUT = 0.20
LONG_RUN_CEILING = 1.0 - TAKEOUT

# これ未満のレース数では回収率の数値を出さない。
# 1日36レースなので、1開催日だけでは足りない = 複数日を積み上げてから出る。
MIN_RACES_FOR_ROI = 50


def unit_returns(picks_and_races) -> np.ndarray:
    """1レース1単位を ◎ に賭けたときの収益列。

    picks_and_races: [(◎の馬番, race)] の並び。
    払戻は matrix の `tan` ({馬番: 払戻円})。100円 = 1単位。
    的中しなければ 0、当たれば 払戻/100。
    """
    out = []
    for pick, race in picks_and_races:
        tan = race.get("tan") or {}
        pay = tan.get(pick)
        out.append((float(pay) / 100.0) if pay else 0.0)
    return np.asarray(out, dtype=np.float64)


def summarize(returns: np.ndarray, *, seed: int = 0) -> dict:
    """回収率と、その数値をそのまま信じてはいけない理由を一緒に返す。

    戻り値の `enough` が False のとき、UI は **数値を出さない**。
    """
    n = int(returns.size)
    if n == 0:
        return {"races": 0, "enough": False, "roi": None, "ci": None,
                "hits": 0, "hit_rate": None, "top_share": None,
                "long_run_ceiling": LONG_RUN_CEILING,
                "min_races": MIN_RACES_FOR_ROI}
    hits = int((returns > 0).sum())
    roi = float(returns.mean())
    total = float(returns.sum())
    # 最大配当1本が全体のどれだけを占めるか。0.5 を超えるなら
    # 「回収率」はその1本の話であって、AI の性能の話ではない。
    top_share = (float(returns.max()) / total) if total > 0 else None
    enough = n >= MIN_RACES_FOR_ROI
    ci = bootstrap_roi_ci(returns, seed=seed) if enough else None
    return {
        "races": n,
        "enough": enough,
        "roi": round(roi, 4),
        "ci": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        "hits": hits,
        "hit_rate": round(hits / n, 4),
        "top_share": None if top_share is None else round(top_share, 4),
        "long_run_ceiling": LONG_RUN_CEILING,
        "min_races": MIN_RACES_FOR_ROI,
    }


def indistinguishable(a: dict, b: dict) -> bool:
    """2つの回収率の差が誤差の範囲か (信頼区間が重なるか)。

    重なっているのに順位を確定させると、運の差を実力の差として見せることになる。
    """
    if not (a and b and a.get("ci") and b.get("ci")):
        return True
    lo_a, hi_a = a["ci"]
    lo_b, hi_b = b["ci"]
    return not (hi_a < lo_b or hi_b < lo_a)


def rank(entries: list[dict], key: str = "roi_stats") -> None:
    """回収率で順位を振る。**区間が重なる相手は同順位**にする。

    entries は roi_stats を持つ dict の並び (破壊的に "roi_rank" を付ける)。
    数値を出せない (enough=False) entry には順位を付けない。
    """
    # 基準 (1番人気AI) は順位を付けない — 競う相手ではなく比較対象。
    # ただし回収率の値そのものは出す (控除率がそのまま出ることの実例になる)。
    scored = [e for e in entries
              if (e.get(key) or {}).get("enough") and not e.get("is_baseline")]
    scored.sort(key=lambda e: -(e[key]["roi"] or 0.0))
    for e in entries:
        e["roi_rank"] = None
        e["roi_tied_with_leader"] = None
    rank_no = 0
    prev = None
    for i, e in enumerate(scored):
        if prev is not None and indistinguishable(prev[key], e[key]):
            e["roi_rank"] = prev["roi_rank"]        # 誤差の範囲 → 同順位
        else:
            rank_no = i + 1
            e["roi_rank"] = rank_no
            prev = e
        e["roi_tied_with_leader"] = (
            indistinguishable(scored[0][key], e[key]) if scored else None)
