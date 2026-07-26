"""予想ビルダーの評価基盤 (Phase 2): ある設定 (config) を過去レースでバックテストする。

model.predict で各レースの ◎ (rank 1) を出し、その的中率 (勝率/複勝率) と単勝回収率を、
TRAIN (split_date より前) / HOLDOUT (split_date 以降) に分けて集計する。過学習を見抜くため
両期間を分離して出すのが要点 (Phase 3 の自動探索での採用判定はこの OOS 成績で行う)。

母集団・払戻・オッズ信頼性は keiba_bridge / scripts.backtest と同じ規律 (read-only)。
"""

from __future__ import annotations

from . import model
from .keiba_bridge import open_conn, _ensure_keiba_on_path


def _blank() -> dict:
    return {"n": 0, "wins": 0, "top3": 0, "ret_sum": 0.0, "ret_n": 0}


def _add(acc: dict, won: int, top3: int, ret: float | None) -> None:
    acc["n"] += 1
    acc["wins"] += won
    acc["top3"] += top3
    if ret is not None:
        acc["ret_sum"] += ret
        acc["ret_n"] += 1


def _summ(acc: dict) -> dict:
    n = acc["n"]
    return {
        "n": n,
        "win_rate": round(acc["wins"] / n, 4) if n else None,
        "top3_rate": round(acc["top3"] / n, 4) if n else None,
        "roi": round(acc["ret_sum"] / acc["ret_n"], 4) if acc["ret_n"] else None,
        "roi_n": acc["ret_n"],
    }


def evaluate(cfg: dict, from_date: str, to_date: str, split_date: str) -> dict:
    """config を [from_date, to_date] で backtest し、TRAIN/HOLDOUT/全体の成績を返す。

    戻り: {"split_date","config","overall","train","holdout"}。
    各成績 = {n, win_rate, top3_rate, roi, roi_n}。◎ = config が付けた rank 1。
    """
    _ensure_keiba_on_path()
    from scripts.backtest import (  # type: ignore
        get_payout_row, horses_for_race, list_races, payout_from_row,
        popularity_config, race_odds_untrusted,
    )

    max_age = popularity_config().get("max_snapshot_age_min")
    cache: dict = {}
    overall, train, holdout = _blank(), _blank(), _blank()

    with open_conn() as conn:
        races = list_races(conn, from_date, to_date, jra_only=True, require_confirmed=True)
        for race in races:
            horses = horses_for_race(conn, race)
            if not horses or not any(h.get("confirmed_order") == 1 for h in horses):
                continue
            preds = model.predict(conn, race, horses, cfg, cache)
            if not preds:
                continue
            pick = preds[0]                       # rank 1 = ◎
            hn = pick["horse_num"]
            co = pick.get("confirmed_order")
            won = 1 if co == 1 else 0
            top3 = 1 if isinstance(co, int) and 1 <= co <= 3 else 0

            untrusted = race_odds_untrusted(horses, race, max_age)
            ret = None
            if not untrusted:
                ret = payout_from_row(get_payout_row(conn, race), hn, "tan") / 100.0

            date = f"{race['race_year']}{race['race_month_day']}"
            _add(overall, won, top3, ret)
            _add(train if date < split_date else holdout, won, top3, ret)

    return {
        "split_date": split_date,
        "config": cfg,
        "overall": _summ(overall),
        "train": _summ(train),
        "holdout": _summ(holdout),
    }
