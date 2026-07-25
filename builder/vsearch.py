"""Phase 3b (大規模版): numpy ベクトル化したバリューベット探索。

純Python版 (search.py) は候補1500で約17分。数万候補×多パターンを回すため、レース内正規化を
numpy 配列にして候補評価を高速化する (速度のためでなく検証規模を上げるため)。

正しさが最優先 (実資産)。eval_value_np は純Python版 matrix.value_stats_prepared と
同一結果になることを test で保証する。
"""

from __future__ import annotations

import random

import numpy as np

from . import matrix as mx


def prepare_numpy(matrix: dict) -> dict:
    """matrix (build_matrix の戻り) を numpy 配列にまとめる。

    レース内正規化 (向き付き z, 欠損/分散0 は 0) を全馬行に対して一括計算し、
    Z(行=全馬, 列=特徴), race offsets, odds/order/tan(馬行ごと), dates/trusted(レースごと)。
    """
    cols = matrix["columns"]
    col_ids = [c["id"] for c in cols]
    cidx = {cid: i for i, cid in enumerate(col_ids)}
    hib = np.array([1.0 if c["hib"] else -1.0 for c in cols], dtype=np.float64)

    Zs, offsets, odds_l, order_l, tan_l, dates, trusted = [], [], [], [], [], [], []
    pos = 0
    for r in matrix["races"]:
        hrows = r["horses"]
        n = len(hrows)
        R = np.full((n, len(col_ids)), np.nan)
        for hi, hr in enumerate(hrows):
            for cid, v in hr["x"].items():
                if v is not None:
                    R[hi, cidx[cid]] = v
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.nanmean(R, axis=0)
            std = np.nanstd(R, axis=0)          # ddof=0 (母集団) — 純Python版と一致
            z = (R - mean) / std
        z = z * hib
        z[~np.isfinite(z)] = 0.0                # 欠損・分散0 は中立(0)
        Zs.append(z)
        offsets.append((pos, pos + n))
        pos += n
        for hr in hrows:
            o = hr.get("odds")
            odds_l.append(o if o is not None else np.nan)
            order_l.append(hr["order"] if isinstance(hr.get("order"), int) else -1)
            tan_l.append(r["tan"].get(hr["num"], 0))
        dates.append(r["date"])
        trusted.append(bool(r.get("trusted")))

    Z = np.vstack(Zs) if Zs else np.zeros((0, len(col_ids)))
    return {
        "Z": Z, "offsets": offsets, "col_ids": col_ids,
        "odds": np.array(odds_l, dtype=np.float64),
        "order": np.array(order_l, dtype=np.int64),
        "tan": np.array(tan_l, dtype=np.float64),
        "dates": dates, "trusted": np.array(trusted, dtype=bool),
        "segs": [r.get("seg", {}) for r in matrix["races"]],
        "fav": [next((hr["num"] for hr in r["horses"] if hr.get("pop") == 1), None)
                for r in matrix["races"]],
        "nums": [[hr["num"] for hr in r["horses"]] for r in matrix["races"]],
    }


# ---------------------------------------------------------------------------
# セグメント (条件) の定義と抽出
# ---------------------------------------------------------------------------
# 粒度: 細 → 粗。データが足りないセグメントは親粒度にフォールバックする。
SEG_LEVELS: list[tuple[str, tuple[str, ...]]] = [
    ("track_surface_distance_condition", ("track", "surface", "distance", "condition")),
    ("track_surface_distance", ("track", "surface", "distance")),
    ("track_surface_bucket_condition", ("track", "surface", "distance_bucket", "condition")),
    ("track_surface_bucket", ("track", "surface", "distance_bucket")),
    ("surface_bucket_condition", ("surface", "distance_bucket", "condition")),
    ("surface_bucket", ("surface", "distance_bucket")),
    ("surface", ("surface",)),
    ("global", ()),
]


def seg_key(seg: dict, fields: tuple[str, ...]) -> str:
    """セグメント属性から粒度 fields のキー文字列を作る。fields 空 = 全体。"""
    if not fields:
        return "ALL"
    return "/".join(str(seg.get(f)) for f in fields)


def race_indices(prep: dict, lo: str, hi: str,
                 fields: tuple[str, ...] = (), key: str | None = None) -> list[int]:
    """[lo,hi] かつ (指定があれば) セグメントキーが一致するレースの index を返す。"""
    out = []
    for i, (dt, sg) in enumerate(zip(prep["dates"], prep["segs"])):
        if not (lo <= dt <= hi):
            continue
        if key is not None and seg_key(sg, fields) != key:
            continue
        out.append(i)
    return out


def segment_keys(prep: dict, fields: tuple[str, ...], lo: str, hi: str) -> dict[str, int]:
    """[lo,hi] における各セグメントキーのレース数。"""
    counts: dict[str, int] = {}
    for dt, sg in zip(prep["dates"], prep["segs"]):
        if lo <= dt <= hi:
            k = seg_key(sg, fields)
            counts[k] = counts.get(k, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 印 (◎ = スコア1位) ベースの評価 — ユーザーの本題「印を打った時の的中率/回収率」
# ---------------------------------------------------------------------------
def eval_pick_idx(prep: dict, w: np.ndarray, idx: list[int]) -> dict:
    """指定レース群で ◎(スコア1位) の 勝率 / 複勝率 / 単勝回収率 を返す。"""
    scores = prep["Z"] @ w
    order, tan, odds = prep["order"], prep["tan"], prep["odds"]
    n = wins = top3 = 0
    stake = ret = 0.0
    roi_n = 0
    for i in idx:
        s, e = prep["offsets"][i]
        sc = scores[s:e]
        if sc.size == 0:
            continue
        j = s + int(np.argmax(sc))
        n += 1
        o = order[j]
        won = 1 if o == 1 else 0
        wins += won
        top3 += 1 if 1 <= o <= 3 else 0
        if prep["trusted"][i]:            # オッズ不信頼レースは回収率に含めない
            stake += 1.0
            roi_n += 1
            ret += (tan[j] / 100.0) if won else 0.0
    return {"n": n,
            "win_rate": round(wins / n, 4) if n else None,
            "top3_rate": round(top3 / n, 4) if n else None,
            "roi": round(ret / stake, 4) if stake else None,
            "roi_n": roi_n}


def eval_value_idx(prep: dict, w: np.ndarray, temperature: float, ev_threshold: float,
                   idx: list[int]) -> dict:
    """指定レース群でのバリューベット成績 (EV=確率×オッズ≥閾値 の馬を単勝で購入)。"""
    scores = prep["Z"] @ w
    t = max(temperature, 1e-6)
    bets = hits = races = 0
    stake = ret = 0.0
    order, odds, tan = prep["order"], prep["odds"], prep["tan"]
    for i in idx:
        if not prep["trusted"][i]:
            continue
        s, e = prep["offsets"][i]
        sc = scores[s:e]
        if sc.size == 0:
            continue
        ex = np.exp((sc - sc.max()) / t)
        p = ex / ex.sum()
        od = odds[s:e]
        sel = (p * od >= ev_threshold) & np.isfinite(od)
        k = int(sel.sum())
        if k:
            bets += k
            stake += k
            won = sel & (order[s:e] == 1)
            hits += int(won.sum())
            ret += float(tan[s:e][won].sum()) / 100.0
            races += 1
    return _summ(bets, hits, stake, ret, races)


def favorite_pick_idx(prep: dict, idx: list[int]) -> dict:
    """ベースライン: そのレース群で市場1番人気を ◎ とした場合の 勝率/複勝率/回収率。"""
    n = wins = top3 = roi_n = 0
    stake = ret = 0.0
    for i in idx:
        fav = prep["fav"][i]
        if fav is None:
            continue
        s, _e = prep["offsets"][i]
        j = s + prep["nums"][i].index(fav)
        n += 1
        o = prep["order"][j]
        won = 1 if o == 1 else 0
        wins += won
        top3 += 1 if 1 <= o <= 3 else 0
        if prep["trusted"][i]:
            stake += 1.0
            roi_n += 1
            ret += (prep["tan"][j] / 100.0) if won else 0.0
    return {"n": n,
            "win_rate": round(wins / n, 4) if n else None,
            "top3_rate": round(top3 / n, 4) if n else None,
            "roi": round(ret / stake, 4) if stake else None,
            "roi_n": roi_n}


def weights_to_vec(prep: dict, weights: dict[str, float]) -> np.ndarray:
    idx = {cid: i for i, cid in enumerate(prep["col_ids"])}
    w = np.zeros(len(prep["col_ids"]), dtype=np.float64)
    for cid, val in weights.items():
        if cid in idx:
            w[idx[cid]] = val
    return w


def _summ(bets, hits, stake, ret, races):
    return {"races_with_bet": races, "bets": int(bets),
            "hit_rate": round(hits / bets, 4) if bets else None,
            "roi": round(ret / stake, 4) if stake else None,
            "profit_units": round(ret - stake, 2)}


def eval_value_np(prep: dict, w: np.ndarray, temperature: float, ev_threshold: float,
                  lo: str, hi: str) -> dict:
    """[lo,hi] のレースに対するバリューベット成績 (ベクトル化)。"""
    return eval_value_idx(prep, w, temperature, ev_threshold,
                          race_indices(prep, lo, hi))


def favorite_np(prep: dict, lo: str, hi: str) -> dict:
    bets = hits = 0
    stake = ret = 0.0
    for (s, e), dt, tr, nums, fav in zip(prep["offsets"], prep["dates"], prep["trusted"],
                                         prep["nums"], prep["fav"]):
        if not tr or fav is None or not (lo <= dt <= hi):
            continue
        j = s + nums.index(fav)
        bets += 1
        stake += 1.0
        won = prep["order"][j] == 1
        hits += int(won)
        ret += (prep["tan"][j] / 100.0) if won else 0.0
    return {"bets": bets, "hit_rate": round(hits / bets, 4) if bets else None,
            "roi": round(ret / stake, 4) if stake else None}


def _gen_weights(col_ids, rng, choices) -> dict[str, float]:
    w = {}
    for cid in col_ids:
        v = rng.choice(choices)
        if v != 0:
            w[cid] = float(v)
    return w


def run_search_np(prep: dict, *, train, valid, test, final=None,
                  n_candidates=20000, min_bets=200,
                  weight_choices=(-1.0, -0.5, 0.0, 0.0, 0.0, 0.5, 1.0),
                  temperatures=(0.5, 1.0, 2.0), ev_thresholds=(1.0, 1.1, 1.2, 1.3),
                  top_k=20, seed=0) -> dict:
    """大規模ランダム探索。TRAINで探索→VALIDで選択→TEST(+final)で最終評価。"""
    rng = random.Random(seed)
    qualified = []
    n_tried = 0
    for _ in range(n_candidates):
        wd = _gen_weights(prep["col_ids"], rng, weight_choices)
        if not wd:
            continue
        n_tried += 1
        t = rng.choice(temperatures)
        ev = rng.choice(ev_thresholds)
        w = weights_to_vec(prep, wd)
        s_tr = eval_value_np(prep, w, t, ev, *train)
        if s_tr["bets"] < min_bets or s_tr["roi"] is None:
            continue
        s_va = eval_value_np(prep, w, t, ev, *valid)
        if s_va["bets"] < min_bets or s_va["roi"] is None:
            continue
        qualified.append({"weights": wd, "temperature": t, "ev_threshold": ev,
                          "train": s_tr, "valid": s_va})

    qualified.sort(key=lambda q: q["valid"]["roi"], reverse=True)
    top = qualified[:top_k]
    for q in top:
        w = weights_to_vec(prep, q["weights"])
        q["test"] = eval_value_np(prep, w, q["temperature"], q["ev_threshold"], *test)
        if final:
            q["final"] = eval_value_np(prep, w, q["temperature"], q["ev_threshold"], *final)

    baselines = {"favorite_test": favorite_np(prep, *test)}
    if final:
        baselines["favorite_final"] = favorite_np(prep, *final)
    return {"n_candidates": n_candidates, "n_tried": n_tried, "n_qualified": len(qualified),
            "expected_false_positives_at_valid": round(len(qualified) * 0.05, 1),
            "min_bets": min_bets, "baselines": baselines, "top": top,
            "periods": {"train": train, "valid": valid, "test": test, "final": final}}
