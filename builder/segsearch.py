"""Phase 3c: セグメント(条件)別の重み探索 — ユーザーの本題。

「新潟芝1800m良ではどの重み付けで印を打つと的中率/回収率が良いか」を、**全粒度 × 全目的関数**で
一度に検証する。範囲を勝手に狭めない (目的関数を選ばせない・粒度を1つに絞らない)。

- 目的関数: 勝率(win_rate) / 複勝率(top3_rate) / 単勝回収率(roi) の **3つ全部**
- 粒度: vsearch.SEG_LEVELS の **8段階全部** (細=場×芝ダ×距離×馬場 → 粗=全体)
- 評価は ◎(スコア1位) ベース = 「印を打った時」の成績

過学習ガード (実資産がかかるため厳格):
1. 3分割 + ウォークフォワード: TRAIN で候補生成 → **VALID で選択** → **TEST と FINAL の
   両方で再現**したものだけ採用 (片方だけの好成績は不採用)。
2. 二重ベースライン超え: そのセグメントの ①市場1番人気 ②全体最適重み の両方を OOS で上回ること。
3. サンプル数ゲート: 全期間で min_races 未満のセグメントは「データ不足」として探索対象外
   (階層フォールバックで親粒度の重みを使う)。
4. 多重比較の記録: 試した (セグメント×候補) 数と期待偽陽性を出力に必ず含める。

実装は完全ベクトル化 (レースごとの ◎ 抽出も bincount 集計も numpy) なので数万候補が回せる。
"""

from __future__ import annotations

import random

import numpy as np

from .vsearch import SEG_LEVELS, favorite_pick_idx, seg_key, weights_to_vec

OBJECTIVES = ("win_rate", "top3_rate", "roi")


def _race_geometry(prep: dict):
    starts = np.array([s for s, _e in prep["offsets"]], dtype=np.int64)
    counts = np.array([e - s for s, e in prep["offsets"]], dtype=np.int64)
    race_of_row = np.repeat(np.arange(len(starts)), counts)
    return starts, counts, race_of_row


def _picks(scores: np.ndarray, starts, counts, race_of_row) -> np.ndarray:
    """各レースの ◎ (スコア最大) の行 index をベクトル化して返す。"""
    max_per_race = np.maximum.reduceat(scores, starts)
    expanded = np.repeat(max_per_race, counts)
    hit = np.flatnonzero(scores == expanded)
    races_hit = race_of_row[hit]
    _u, first_pos = np.unique(races_hit, return_index=True)
    return hit[first_pos]


def _level_codes(prep: dict, fields: tuple[str, ...]):
    """粒度 fields におけるレースごとのセグメント整数コードとキー一覧。"""
    keys: dict[str, int] = {}
    codes = np.empty(len(prep["dates"]), dtype=np.int64)
    for i, sg in enumerate(prep["segs"]):
        k = seg_key(sg, fields)
        if k not in keys:
            keys[k] = len(keys)
        codes[i] = keys[k]
    return codes, list(keys.keys())


def _period_masks(prep: dict, periods: dict[str, tuple[str, str]]):
    dates = np.array(prep["dates"])
    return {name: (dates >= lo) & (dates <= hi) for name, (lo, hi) in periods.items()}


def _agg(codes, mask, n_seg, won, top3, ret, trusted):
    """セグメント別に n/wins/top3/回収 を bincount 集計する。"""
    m = mask
    c = codes[m]
    n = np.bincount(c, minlength=n_seg).astype(np.float64)
    w = np.bincount(c, weights=won[m], minlength=n_seg)
    t3 = np.bincount(c, weights=top3[m], minlength=n_seg)
    tr = trusted[m]
    ct = codes[m][tr]
    stake = np.bincount(ct, minlength=n_seg).astype(np.float64)
    retsum = np.bincount(ct, weights=ret[m][tr], minlength=n_seg)
    return n, w, t3, stake, retsum


def _metric(name, n, wins, top3, stake, retsum):
    with np.errstate(invalid="ignore", divide="ignore"):
        if name == "win_rate":
            return np.where(n > 0, wins / np.maximum(n, 1), np.nan)
        if name == "top3_rate":
            return np.where(n > 0, top3 / np.maximum(n, 1), np.nan)
        return np.where(stake > 0, retsum / np.maximum(stake, 1), np.nan)


def _gen_weights(col_ids, rng, choices) -> dict[str, float]:
    w = {}
    for cid in col_ids:
        v = rng.choice(choices)
        if v != 0:
            w[cid] = float(v)
    return w


def run_segment_search(prep: dict, *, train, valid, test, final,
                       n_candidates: int = 5000, min_races: int = 30,
                       weight_choices=(-1.0, -0.5, 0.0, 0.0, 0.0, 0.5, 1.0),
                       levels=SEG_LEVELS, objectives=OBJECTIVES,
                       seed: int = 0, progress_every: int = 500) -> dict:
    """全粒度 × 全目的関数でセグメント別に重みを探索し、OOS 再現したものだけ採用する。

    戻り: {"levels": {level_name: {"segments": {key: {objective: {...}}}}},
           "summary": {...多重比較の記録...}}
    """
    starts, counts, race_of_row = _race_geometry(prep)
    periods = {"train": train, "valid": valid, "test": test, "final": final}
    masks = _period_masks(prep, periods)
    order, tan, trusted = prep["order"], prep["tan"], np.asarray(prep["trusted"])

    lv = []
    for name, fields in levels:
        codes, keys = _level_codes(prep, fields)
        lv.append({"name": name, "fields": fields, "codes": codes, "keys": keys,
                   "n_seg": len(keys)})

    # 候補プールを1回だけ生成 (全セグメントで同じ候補を評価 → 公平かつ効率的)
    rng = random.Random(seed)
    cand = []
    for _ in range(n_candidates):
        wd = _gen_weights(prep["col_ids"], rng, weight_choices)
        if wd:
            cand.append(wd)

    # --- Pass 1: 各候補を全セグメント×全期間で集計し、VALID 最良を追跡 ---
    best = {L["name"]: {o: {"metric": np.full(L["n_seg"], -np.inf),
                            "cand": np.full(L["n_seg"], -1, dtype=np.int64)}
                        for o in objectives} for L in lv}
    stats_cache: dict = {}

    for ci, wd in enumerate(cand):
        w = weights_to_vec(prep, wd)
        scores = prep["Z"] @ w
        pick = _picks(scores, starts, counts, race_of_row)
        po = order[pick]
        won = (po == 1).astype(np.float64)
        top3 = ((po >= 1) & (po <= 3)).astype(np.float64)
        ret = np.where(won > 0, tan[pick] / 100.0, 0.0)

        for L in lv:
            codes, n_seg = L["codes"], L["n_seg"]
            agg = {p: _agg(codes, masks[p], n_seg, won, top3, ret, trusted)
                   for p in ("train", "valid")}
            n_tr = agg["train"][0]
            n_va = agg["valid"][0]
            ok = (n_tr >= min_races) & (n_va >= min_races)
            for o in objectives:
                mv = _metric(o, *agg["valid"])
                cand_ok = ok & np.isfinite(mv)
                b = best[L["name"]][o]
                better = cand_ok & (mv > b["metric"])
                b["metric"] = np.where(better, mv, b["metric"])
                b["cand"] = np.where(better, ci, b["cand"])
        if progress_every and (ci + 1) % progress_every == 0:
            print(f"  pass1 {ci+1}/{len(cand)}", flush=True)

    # --- Pass 2: 勝者候補のみ TEST/FINAL を評価し、採用判定 ---
    def full_stats(ci: int):
        if ci in stats_cache:
            return stats_cache[ci]
        w = weights_to_vec(prep, cand[ci])
        scores = prep["Z"] @ w
        pick = _picks(scores, starts, counts, race_of_row)
        po = order[pick]
        won = (po == 1).astype(np.float64)
        top3 = ((po >= 1) & (po <= 3)).astype(np.float64)
        ret = np.where(won > 0, tan[pick] / 100.0, 0.0)
        stats_cache[ci] = (won, top3, ret)
        return stats_cache[ci]

    # 全体(global)の各目的関数における最良重み = セグメント専用重みが超えるべき基準
    gl = next(L for L in lv if L["name"] == "global")
    global_best = {}
    for o in objectives:
        ci = int(gl_ci) if (gl_ci := best["global"][o]["cand"][0]) >= 0 else -1
        global_best[o] = ci

    out_levels: dict = {}
    n_tested = 0
    for L in lv:
        segs_out: dict = {}
        codes = L["codes"]
        for si, key in enumerate(L["keys"]):
            seg_mask = codes == si
            idx_test = np.flatnonzero(seg_mask & masks["test"]).tolist()
            idx_final = np.flatnonzero(seg_mask & masks["final"]).tolist()
            fav_test = favorite_pick_idx(prep, idx_test)
            fav_final = favorite_pick_idx(prep, idx_final)
            per_obj: dict = {}
            for o in objectives:
                ci = int(best[L["name"]][o]["cand"][si])
                if ci < 0:
                    per_obj[o] = {"status": "insufficient_data"}
                    continue
                n_tested += 1
                won, top3, ret = full_stats(ci)
                res = {}
                for p in ("train", "valid", "test", "final"):
                    n, wi, t3, st, rs = _agg(codes, seg_mask & masks[p], L["n_seg"],
                                             won, top3, ret, trusted)
                    res[p] = {"n": int(n[si]),
                              "win_rate": None if n[si] == 0 else round(float(wi[si] / n[si]), 4),
                              "top3_rate": None if n[si] == 0 else round(float(t3[si] / n[si]), 4),
                              "roi": None if st[si] == 0 else round(float(rs[si] / st[si]), 4)}
                # 全体最適重みを同セグメントで評価 (専用重みが超えるべき基準②)
                gci = global_best[o]
                gres = {}
                if gci >= 0:
                    gwon, gtop3, gret = full_stats(gci)
                    for p in ("test", "final"):
                        n, wi, t3, st, rs = _agg(codes, seg_mask & masks[p], L["n_seg"],
                                                 gwon, gtop3, gret, trusted)
                        gres[p] = {"n": int(n[si]),
                                   "win_rate": None if n[si] == 0 else round(float(wi[si] / n[si]), 4),
                                   "top3_rate": None if n[si] == 0 else round(float(t3[si] / n[si]), 4),
                                   "roi": None if st[si] == 0 else round(float(rs[si] / st[si]), 4)}

                def better(a, b):
                    return a is not None and b is not None and a > b

                enough = res["test"]["n"] >= min_races and res["final"]["n"] >= min_races
                beats_fav = (better(res["test"][o], fav_test.get(o))
                             and better(res["final"][o], fav_final.get(o)))
                beats_global = (better(res["test"][o], gres.get("test", {}).get(o))
                                and better(res["final"][o], gres.get("final", {}).get(o)))
                adopted = bool(enough and beats_fav and beats_global)
                per_obj[o] = {
                    "status": "ok" if enough else "insufficient_oos",
                    "weights": cand[ci], "periods": res,
                    "favorite": {"test": fav_test, "final": fav_final},
                    "global_weight_in_segment": gres,
                    "beats_favorite_both": bool(beats_fav),
                    "beats_global_both": bool(beats_global),
                    "adopted": adopted,
                }
            segs_out[key] = per_obj
        out_levels[L["name"]] = {"fields": list(L["fields"]), "segments": segs_out}

    adopted_count = sum(1 for L in out_levels.values() for s in L["segments"].values()
                        for o in s.values() if isinstance(o, dict) and o.get("adopted"))
    return {
        "levels": out_levels,
        "summary": {
            "n_candidates": len(cand),
            "objectives": list(objectives),
            "min_races": min_races,
            "periods": {k: list(v) for k, v in periods.items()},
            "n_segment_objective_tested": n_tested,
            "expected_false_positives_at_valid": round(n_tested * 0.05, 1),
            "n_adopted": adopted_count,
        },
    }
