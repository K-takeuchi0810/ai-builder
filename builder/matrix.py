"""Phase 3a: 特徴量行列の事前計算 + ディスクキャッシュ。

compute_features は 1 頭ごとに騎手・血統等を DB 集計するため高コスト。Phase 3 の重み自動探索は
「同じ特徴量を重みだけ変えて何百回も評価」するので、毎回 DB を叩くと非現実的に遅い。

そこで候補特徴量セット (columns) について、期間内の全 (レース×馬) の特徴量ベクトル・着順・
単勝払戻・セグメント属性を **一度だけ** 計算して JSON にキャッシュする。以降の探索
(score_race_columns / evaluate_matrix) は行列上の純粋計算だけで回るため高速。

read-only: keiba-yosou は model 経由 (compute_features / horse_past_runs) でのみ参照。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from . import axes as ax
from . import config, model
from . import normalize as nrm
from .keiba_bridge import open_conn, _ensure_keiba_on_path

# v2: per-horse に decimal odds(win_odds/10) と win_popularity を追加 (バリューベット EV 用)。
MATRIX_VERSION = 2
_CACHE_DIR = Path(config.__file__).resolve().parent.parent / "out" / "matrix"


def _col_id(spec: dict) -> str:
    key = spec["key"]
    feat = model.FEATURES.get(key)
    if feat and feat.kind == "aggregate":
        lb = spec.get("lookback")
        m = ",".join(sorted(spec.get("match") or []))
        return f"{key}|lb={lb}|m={m}"
    return key


def _columns(specs: list[dict]) -> list[dict]:
    from . import labels as lb
    cols = []
    for s in specs:
        feat = model.FEATURES.get(s.get("key"))
        if feat is None:
            continue
        # label には **セル (一致条件・さかのぼる範囲) まで** 入れる。同じ集計対象の
        # 別セルを複数選んだとき、寄与の行が同名になって区別できなくなるのを防ぐ。
        # _col_hash は id のみを見るのでラベル変更で行列キャッシュは無効化されない。
        cols.append({"id": _col_id(s), "key": feat.key, "kind": feat.kind,
                     "hib": feat.higher_is_better,
                     "label": lb.column_label(feat.key, s.get("match"), s.get("lookback")),
                     "lookback": s.get("lookback"), "match": s.get("match") or []})
    return cols


def _seg(race: dict) -> dict:
    return {
        "track": str(race.get("track_code")),
        "distance": race.get("distance"),
        "distance_bucket": ax.distance_bucket(race.get("distance")),
        "surface": model._surface(race.get("track_type_code")),
        "condition": ax.condition_key(model._surface(race.get("track_type_code")),
                                      race.get("turf_condition"), race.get("dirt_condition")),
        "month": (race.get("race_month_day", "") or "")[:2].lstrip("0") or "unknown",
    }


def _tan_payouts(payout_row: dict | None) -> dict[str, int]:
    """払戻行から単勝の {馬番: 払戻円} を取り出す (同着最大3)。"""
    out: dict[str, int] = {}
    if not payout_row:
        return out
    for i in (1, 2, 3):
        num = payout_row.get(f"tan_horse_num{i}")
        pay = payout_row.get(f"tan_payout{i}")
        if num and pay:
            out[str(num)] = int(pay)
    return out


def _cache_path(from_date: str, to_date: str, cols: list[dict]) -> Path:
    ids = ",".join(c["id"] for c in cols)
    h = hashlib.sha1(f"{MATRIX_VERSION}|{from_date}|{to_date}|{ids}".encode()).hexdigest()[:16]
    return _CACHE_DIR / f"matrix_{from_date}_{to_date}_{h}.json"


def merge_matrices(matrices: list[dict]) -> dict:
    """同一 columns の複数行列 (例: 年別) を 1 つに結合する。"""
    if not matrices:
        return {"columns": [], "races": []}
    ids = [c["id"] for c in matrices[0]["columns"]]
    for m in matrices[1:]:
        if [c["id"] for c in m["columns"]] != ids:
            raise ValueError("columns mismatch: cannot merge matrices")
    races = [r for m in matrices for r in m["races"]]
    return {"from": min(m["from"] for m in matrices),
            "to": max(m["to"] for m in matrices),
            "columns": matrices[0]["columns"], "races": races}


def load_years(years: list[int], specs: list[dict]) -> dict:
    """年別キャッシュ (build_matrix 済み) を読み込んで結合。未構築の年があれば構築される。"""
    mats = [build_matrix(f"{y}0101", f"{y}1231", specs) for y in years]
    return merge_matrices(mats)


def _col_hash(cols: list[dict]) -> str:
    ids = ",".join(c["id"] for c in cols)
    return hashlib.sha1(f"{MATRIX_VERSION}|{ids}".encode()).hexdigest()[:16]


def _months(from_date: str, to_date: str) -> list[str]:
    """[from,to] を月 (YYYYMM) の並びに展開。"""
    y, m = int(from_date[:4]), int(from_date[4:6])
    ey, em = int(to_date[:4]), int(to_date[4:6])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _races_for_range(conn, lo: str, hi: str, cols: list[dict],
                     need_compute: bool, need_past: bool, max_age) -> list[dict]:
    """[lo,hi] の JRA 確定レースを特徴量行列レコードに変換 (build_matrix の中核)。"""
    from scripts.backtest import (  # type: ignore
        get_payout_row, horses_for_race, list_races, race_odds_untrusted,
    )
    out: list[dict] = []
    cache: dict = {}
    for race in list_races(conn, lo, hi, jra_only=True, require_confirmed=True):
        horses = horses_for_race(conn, race)
        if not horses or not any(h.get("confirmed_order") == 1 for h in horses):
            continue
        before = f"{race.get('race_year')}{race.get('race_month_day')}"
        hrows = []
        for h in horses:
            feats = {}
            if need_compute:
                fkey = ("cf", h.get("blood_register_num"), before, str(h.get("horse_num")))
                if fkey not in cache:
                    cache[fkey] = model._compute_features(conn, h, race, cache)
                feats = cache[fkey]
            past = []
            if need_past:
                pkey = ("past50", h.get("blood_register_num"), before)
                if pkey not in cache:
                    cache[pkey] = model._past_runs(conn, h.get("blood_register_num"), before,
                                                   cache=cache) \
                        if h.get("blood_register_num") else []
                past = cache[pkey]
            x: dict[str, float | None] = {}
            for c in cols:
                feat = model.FEATURES[c["key"]]
                if c["kind"] == "compute":
                    x[c["id"]] = feat.metric(feats)
                elif c["kind"] == "aggregate":
                    x[c["id"]] = model._aggregate_value(feat, past, race, c["match"], c["lookback"])
                else:
                    x[c["id"]] = feat.metric(h)
            wo = model._num(h.get("win_odds"))
            odds = (wo / 10.0) if (wo and wo > 0) else None   # win_odds/10 = 10進オッズ (証跡参照)
            hrows.append({"num": str(h.get("horse_num")), "order": h.get("confirmed_order"),
                          "odds": odds, "pop": model._num(h.get("win_popularity")), "x": x})
        out.append({
            "date": before, "seg": _seg(race), "horses": hrows,
            "tan": _tan_payouts(get_payout_row(conn, race)),
            "trusted": not race_odds_untrusted(horses, race, max_age),
        })
    return out


def build_matrix(from_date: str, to_date: str, specs: list[dict],
                 rebuild: bool = False) -> dict:
    """候補特徴量 specs について特徴量行列を構築。**月次チェックポイント**で中断に強い。

    - 完成済みの範囲キャッシュ (out/matrix/matrix_*.json) があればそれを読む (高速・後方互換)。
    - 無ければ **月 (YYYYMM) 単位で構築し、各月を out/matrix/months/ に逐次保存**。途中でクラッシュ
      しても次回は未完の月から再開でき、最大でも「その月ぶん (~15分)」しか失わない。
    - 全月そろったら範囲キャッシュを書き出して返す。

    戻り: {"from","to","columns","races":[{date, seg, horses:[{num,order,odds,pop,x}],
           tan:{num:yen}, trusted}]}
    """
    cols = _columns(specs)
    range_path = _cache_path(from_date, to_date, cols)
    if range_path.exists() and not rebuild:
        return json.loads(range_path.read_text(encoding="utf-8"))

    _ensure_keiba_on_path()
    from scripts.backtest import popularity_config  # type: ignore
    max_age = popularity_config().get("max_snapshot_age_min")
    need_compute = any(c["kind"] == "compute" for c in cols)
    need_past = any(c["kind"] == "aggregate" for c in cols)
    chash = _col_hash(cols)

    month_dir = _CACHE_DIR / "months"
    all_races: list[dict] = []
    month_dir.mkdir(parents=True, exist_ok=True)
    with open_conn() as conn:
        for ym in _months(from_date, to_date):
            mp = month_dir / f"m_{ym}_{chash}.json"
            if mp.exists() and not rebuild:
                all_races.extend(json.loads(mp.read_text(encoding="utf-8"))["races"])
                continue
            lo, hi = f"{ym}01", f"{ym}31"
            races = _races_for_range(conn, lo, hi, cols, need_compute, need_past, max_age)
            tmp = mp.with_suffix(".json.tmp")             # 原子的書き込み (部分ファイルを残さない)
            tmp.write_text(json.dumps({"month": ym, "races": races}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(mp)
            all_races.extend(races)

    # 月キャッシュはフル月ぶん保存するが、返却は要求範囲 [from,to] にクリップする。
    races_out = [r for r in all_races if from_date <= r["date"] <= to_date]
    matrix = {"from": from_date, "to": to_date, "columns": cols, "races": races_out}
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = range_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    tmp.replace(range_path)
    return matrix


# ---------------------------------------------------------------------------
# 行列上の高速評価 (DB 非依存・純粋)
# ---------------------------------------------------------------------------
def score_race_columns(hrows: list[dict], columns: list[dict],
                       weights: dict[str, float]) -> list[tuple[str, float]]:
    """行列の 1 レース分 (hrows) を weights でスコアリング。(馬番, スコア) 降順。純粋関数。"""
    nums = [hr["num"] for hr in hrows]
    scores = {n: 0.0 for n in nums}
    col_by_id = {c["id"]: c for c in columns}
    for cid, w in weights.items():
        c = col_by_id.get(cid)
        if c is None or w == 0.0:
            continue
        present = {hr["num"]: hr["x"].get(cid) for hr in hrows}
        vals = [v for v in present.values() if v is not None]
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        if var == 0:
            continue
        std = var ** 0.5
        direction = 1.0 if c["hib"] else -1.0
        for n in nums:
            v = present[n]
            if v is None:
                continue
            scores[n] += w * direction * ((v - mean) / std)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _blank():
    return {"n": 0, "wins": 0, "top3": 0, "ret_sum": 0.0, "ret_n": 0}


def _summ(a: dict) -> dict:
    n = a["n"]
    return {"n": n,
            "win_rate": round(a["wins"] / n, 4) if n else None,
            "top3_rate": round(a["top3"] / n, 4) if n else None,
            "roi": round(a["ret_sum"] / a["ret_n"], 4) if a["ret_n"] else None,
            "roi_n": a["ret_n"]}


def evaluate_matrix(matrix: dict, weights: dict[str, float], split_date: str,
                    races: list[dict] | None = None) -> dict:
    """行列上で weights を backtest。TRAIN/HOLDOUT/overall の成績を返す (DB 非依存)。"""
    cols = matrix["columns"]
    rs = races if races is not None else matrix["races"]
    overall, train, holdout = _blank(), _blank(), _blank()
    for r in rs:
        ranked = score_race_columns(r["horses"], cols, weights)
        if not ranked:
            continue
        pick = ranked[0][0]
        order = next((hr["order"] for hr in r["horses"] if hr["num"] == pick), None)
        won = 1 if order == 1 else 0
        top3 = 1 if isinstance(order, int) and 1 <= order <= 3 else 0
        ret = (r["tan"].get(pick, 0) / 100.0) if r["trusted"] else None
        for acc in (overall, train if r["date"] < split_date else holdout):
            acc["n"] += 1
            acc["wins"] += won
            acc["top3"] += top3
            if ret is not None:
                acc["ret_sum"] += ret
                acc["ret_n"] += 1
    return {"overall": _summ(overall), "train": _summ(train), "holdout": _summ(holdout)}


# ---------------------------------------------------------------------------
# バリューベット評価 (EV = 推定確率 × オッズ ≥ 閾値 の馬だけ単勝で買う)
# ---------------------------------------------------------------------------
def _softmax(pairs: list[tuple[str, float]], temperature: float) -> dict[str, float]:
    """(馬番, スコア) → レース内確率 (Σ=1)。temperature が小さいほど尖る。"""
    if not pairs:
        return {}
    t = max(temperature, 1e-6)
    mx = max(s for _, s in pairs)
    exps = {n: math.exp((s - mx) / t) for n, s in pairs}
    z = sum(exps.values()) or 1.0
    return {n: e / z for n, e in exps.items()}


def _value_blank():
    return {"races": 0, "bets": 0, "hits": 0, "stake": 0.0, "ret": 0.0}


def _value_summ(a: dict) -> dict:
    b = a["bets"]
    return {
        "races_with_bet": a["races"],
        "bets": b,
        "hit_rate": round(a["hits"] / b, 4) if b else None,
        "roi": round(a["ret"] / a["stake"], 4) if a["stake"] else None,
        "profit_units": round(a["ret"] - a["stake"], 2),
    }


def evaluate_value_matrix(matrix: dict, weights: dict[str, float], split_date: str,
                          temperature: float = 1.0, ev_threshold: float = 1.0,
                          races: list[dict] | None = None) -> dict:
    """バリューベット方針を行列上で backtest (DB 非依存・純粋)。

    各レースでスコア→softmax確率 p を出し、EV = p × 10進オッズ ≥ ev_threshold の馬だけ
    単勝を 1 単位ずつ購入。実払戻 (tan) で回収。TRAIN/HOLDOUT/overall を分けて返す。
    trusted=False (オッズ不信頼) のレースは購入対象外。
    """
    cols = matrix["columns"]
    rs = races if races is not None else matrix["races"]
    overall, train, holdout = _value_blank(), _value_blank(), _value_blank()

    for r in rs:
        if not r.get("trusted"):
            continue
        ranked = score_race_columns(r["horses"], cols, weights)
        if not ranked:
            continue
        probs = _softmax(ranked, temperature)
        odds_by = {hr["num"]: hr.get("odds") for hr in r["horses"]}
        order_by = {hr["num"]: hr.get("order") for hr in r["horses"]}
        tan = r["tan"]
        acc = train if r["date"] < split_date else holdout
        bet_in_race = False
        for num, _score in ranked:
            odds = odds_by.get(num)
            if odds is None:
                continue
            ev = probs.get(num, 0.0) * odds
            if ev < ev_threshold:
                continue
            won = 1 if order_by.get(num) == 1 else 0
            ret = (tan.get(num, 0) / 100.0) if won else 0.0
            for a in (overall, acc):
                a["bets"] += 1
                a["hits"] += won
                a["stake"] += 1.0
                a["ret"] += ret
            bet_in_race = True
        if bet_in_race:
            overall["races"] += 1
            acc["races"] += 1

    return {"overall": _value_summ(overall), "train": _value_summ(train),
            "holdout": _value_summ(holdout),
            "params": {"temperature": temperature, "ev_threshold": ev_threshold}}


# ---------------------------------------------------------------------------
# 高速探索用: レース内正規化を一度だけ事前計算する (重み探索を軽くする)
# ---------------------------------------------------------------------------
def prepare_races(matrix: dict) -> list[dict]:
    """各レースの各カラムを「向き付き z 値」に事前正規化して返す。

    z は重みに依存しない (レース内の値だけで決まる) ので一度計算すれば全候補で使い回せる。
    戻り各要素: {date, trusted, tan, odds:{num:o}, order:{num:o}, fav:num|None,
                 z:{num:{col_id:zval}}}  (欠損・分散0のカラムは省略=寄与0)。
    """
    cols = matrix["columns"]
    out = []
    for r in matrix["races"]:
        hrows = r["horses"]
        nums = [hr["num"] for hr in hrows]
        z: dict[str, dict[str, float]] = {n: {} for n in nums}
        for c in cols:
            cid = c["id"]
            present = {hr["num"]: hr["x"].get(cid) for hr in hrows}
            # 欠損ポリシーは normalize.py に単一実装 (カバレッジ不足の列はレース内で不使用)
            zs, decision, _n = nrm.race_z(present, c["hib"], len(nums))
            if decision != nrm.USE:
                continue
            for n, zv in zs.items():
                z[n][cid] = zv
        fav = next((hr["num"] for hr in hrows if hr.get("pop") == 1), None)
        out.append({
            "index": len(out),                       # 入力順の位置 (呼び出し側の紐付け用)
            "race_id": r.get("race_id"),             # あれば透過 (日次バッチが付与)
            "date": r["date"], "trusted": r.get("trusted", False), "tan": r["tan"],
            "odds": {hr["num"]: hr.get("odds") for hr in hrows},
            "order": {hr["num"]: hr.get("order") for hr in hrows},
            "fav": fav, "z": z,
        })
    return out


def _score_prepared(zrace: dict, weights: dict[str, float]) -> dict[str, float]:
    scores = {}
    for num, zc in zrace["z"].items():
        s = 0.0
        for cid, w in weights.items():
            zv = zc.get(cid)
            if zv is not None:
                s += w * zv
        scores[num] = s
    return scores


def value_stats_prepared(prep_races: list[dict], weights: dict[str, float],
                         temperature: float, ev_threshold: float) -> dict:
    """事前正規化済みレース集合に対するバリューベット成績 (単一バケット・高速)。"""
    acc = _value_blank()
    for r in prep_races:
        if not r["trusted"]:
            continue
        scores = _score_prepared(r, weights)
        if not scores:
            continue
        probs = _softmax(list(scores.items()), temperature)
        bet_in_race = False
        for num in scores:
            odds = r["odds"].get(num)
            if odds is None:
                continue
            if probs.get(num, 0.0) * odds < ev_threshold:
                continue
            won = 1 if r["order"].get(num) == 1 else 0
            acc["bets"] += 1
            acc["hits"] += won
            acc["stake"] += 1.0
            acc["ret"] += (r["tan"].get(num, 0) / 100.0) if won else 0.0
            bet_in_race = True
        if bet_in_race:
            acc["races"] += 1
    return _value_summ(acc)


def favorite_stats_prepared(prep_races: list[dict]) -> dict:
    """事前正規化済みレースでの市場1番人気・単勝ベースライン。"""
    bets = hits = 0
    stake = ret = 0.0
    for r in prep_races:
        if not r["trusted"] or r["fav"] is None:
            continue
        bets += 1
        stake += 1.0
        won = 1 if r["order"].get(r["fav"]) == 1 else 0
        hits += won
        ret += (r["tan"].get(r["fav"], 0) / 100.0) if won else 0.0
    return {"bets": bets, "hit_rate": round(hits / bets, 4) if bets else None,
            "roi": round(ret / stake, 4) if stake else None}
