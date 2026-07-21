"""Phase 3b: 重み自動探索 (バリューベット × 厳格な out-of-sample 検証)。

過学習を弾くための鉄則:
- **3分割**: 訓練(探索でROI最大化)→ 検証(候補の選択)→ テスト(最終評価は一度だけ。探索にも
  選択にも一切使わない)。さらに 2026 を最新 OOS として併用可。テストの数字だけが「実力」。
- **多重比較の明示**: 試した候補数・生存数・期待偽陽性を必ず記録。数撃てば当たる分を割り引く。
- **人気ベースライン超えを要求**: 市場人気だけの単勝 ROI を上回らないバリュー戦略に価値はない。

探索空間: 各特徴量カラムの重み ∈ weight_choices、softmax temperature、EV 閾値。行列 (matrix.py) 上で
純粋に評価するので DB 非依存で高速。乱数は seed 固定で再現可能。
"""

from __future__ import annotations

import random

from . import matrix as mx

FAR = "99999999"


def _slice(races: list[dict], lo: str, hi: str) -> list[dict]:
    """YYYYMMDD 文字列比較で [lo, hi] のレースを抽出。"""
    return [r for r in races if lo <= r["date"] <= hi]


def _value(cols, races, weights, temperature, ev_threshold) -> dict:
    """レース集合に対するバリューベット成績 (単一バケット)。"""
    res = mx.evaluate_value_matrix({"columns": cols, "races": races}, weights,
                                   split_date=FAR, temperature=temperature,
                                   ev_threshold=ev_threshold)
    return res["train"]           # split=FAR なので全レースが train バケットに入る


def favorite_baseline(races: list[dict]) -> dict:
    """ベースライン: 各レースで市場1番人気を単勝で買う (trusted のみ)。"""
    bets = hits = 0
    stake = ret = 0.0
    for r in races:
        if not r.get("trusted"):
            continue
        fav = next((hr for hr in r["horses"] if hr.get("pop") == 1), None)
        if fav is None:
            continue
        bets += 1
        stake += 1.0
        won = 1 if fav.get("order") == 1 else 0
        hits += won
        ret += (r["tan"].get(fav["num"], 0) / 100.0) if won else 0.0
    return {"bets": bets, "hit_rate": round(hits / bets, 4) if bets else None,
            "roi": round(ret / stake, 4) if stake else None}


def _gen_weights(cols, rng, choices) -> dict[str, float]:
    w = {}
    for c in cols:
        v = rng.choice(choices)
        if v != 0:
            w[c["id"]] = float(v)
    return w


def run_search(matrix: dict, *, train: tuple[str, str], valid: tuple[str, str],
               test: tuple[str, str], final: tuple[str, str] | None = None,
               n_candidates: int = 3000, min_bets: int = 100,
               weight_choices=(-1.0, -0.5, 0.0, 0.5, 1.0),
               temperatures=(0.5, 1.0, 2.0), ev_thresholds=(1.0, 1.1, 1.2, 1.3),
               top_k: int = 10, seed: int = 0) -> dict:
    """バリューベットの重みを探索。TRAINで探索→VALIDで選択→TEST(+final)で最終評価。

    戻り: {n_tried, n_qualified, baselines, top:[{weights,temperature,ev_threshold,
           train,valid,test,final}], expected_false_positives, ...}
    """
    cols = matrix["columns"]
    races = matrix["races"]
    tr = _slice(races, *train)
    va = _slice(races, *valid)
    te = _slice(races, *test)
    fi = _slice(races, *final) if final else []

    rng = random.Random(seed)
    qualified: list[dict] = []
    n_tried = 0
    for _ in range(n_candidates):
        w = _gen_weights(cols, rng, weight_choices)
        if not w:
            continue
        n_tried += 1
        t = rng.choice(temperatures)
        ev = rng.choice(ev_thresholds)
        s_tr = _value(cols, tr, w, t, ev)
        if (s_tr["bets"] or 0) < min_bets or s_tr["roi"] is None:
            continue
        s_va = _value(cols, va, w, t, ev)
        if (s_va["bets"] or 0) < min_bets or s_va["roi"] is None:
            continue
        qualified.append({"weights": w, "temperature": t, "ev_threshold": ev,
                          "train": s_tr, "valid": s_va})

    # 選択は VALID の ROI のみで行う (TEST は見ない)
    qualified.sort(key=lambda q: q["valid"]["roi"], reverse=True)
    top = qualified[:top_k]
    # 選ばれた候補だけ TEST / final を評価 (最終・一度きり)
    for q in top:
        q["test"] = _value(cols, te, q["weights"], q["temperature"], q["ev_threshold"])
        if fi:
            q["final"] = _value(cols, fi, q["weights"], q["temperature"], q["ev_threshold"])

    baselines = {
        "favorite_train": favorite_baseline(tr),
        "favorite_valid": favorite_baseline(va),
        "favorite_test": favorite_baseline(te),
    }
    if fi:
        baselines["favorite_final"] = favorite_baseline(fi)

    return {
        "n_candidates": n_candidates,
        "n_tried": n_tried,
        "n_qualified": len(qualified),
        "expected_false_positives_at_valid": round(len(qualified) * 0.05, 1),
        "min_bets": min_bets,
        "columns": [c["id"] for c in cols],
        "baselines": baselines,
        "top": top,
        "periods": {"train": train, "valid": valid, "test": test, "final": final},
    }
