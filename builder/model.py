"""予想ビルダーの中核 (Phase 1): 設定 → 特徴量 → スコア → 予想。

netkeiba の「予想AIビルダー」に相当。ユーザーが選んだ特徴量 (パラメータ) と重み・集計条件から、
1 レースの出走馬をスコア化して順位付け (◎) する。特徴量は 3 種類:

- compute   … keiba-yosou の compute_features が計算済みの特徴量 (幅。約40候補)。固定集計。
- aggregate … 過去走を「直近N走 × 一致条件」で集計する自前特徴 (深さ)。netkeiba の左右クロス。
- current   … 現在レースの生属性 (人気 等)。

設定 (config) の形:
    {"features": [
        {"key": "jockey_win_rate",  "weight": 1.0},                                  # compute/current
        {"key": "agg_top3_rate",    "weight": 1.0, "lookback": 5, "match": ["surface"]},  # aggregate
        ...
    ]}
- lookback / match は aggregate 特徴のみ有効 (compute/current では無視)。

スコアリングは「レース内で各特徴量を z-score 正規化 → 向き(higher_is_better)を揃える →
重み付き和」。値が無い馬はその特徴で中立 (z=0)。**向き (higher_is_better) は既定の解釈で、
重みを負にすれば反転できる** (Phase 3 の自動探索は符号も含めて探索する)。

keiba-yosou は read-only 参照のみ (compute_features / horse_past_runs)。予想には影響しない。
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass
from typing import Callable

from . import config


def _ensure_keiba_on_path() -> None:
    p = str(config.KEIBA_YOSOU_PATH)
    if p not in sys.path:
        sys.path.insert(0, p)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _surface(track_type_code) -> str:
    """track_type_code → turf/dirt/jump/other (芝10-22 / ダ23-29 / 障51-59)。"""
    n = _num(track_type_code)
    if n is None:
        return "other"
    n = int(n)
    if 10 <= n <= 22:
        return "turf"
    if 23 <= n <= 29:
        return "dirt"
    if 51 <= n <= 59:
        return "jump"
    return "other"


@dataclass(frozen=True)
class Feature:
    key: str
    label: str                              # 日本語表示名
    category: str                           # UI グループ
    kind: str                               # "compute" | "aggregate" | "current"
    higher_is_better: bool                  # 既定の向き (重み符号で反転可)
    metric: Callable[[dict], float | None]  # compute:feats / current:horse / aggregate:1過去走 → 数値


# --- compute 特徴の抽出子 (compute_features の戻り dict から取り出す) --------
def _cf(key: str) -> Callable[[dict], float | None]:
    return lambda feats: _num(feats.get(key))


def _cf_rate(top3_key: str, runs_key: str) -> Callable[[dict], float | None]:
    """count ペア (top3 数 / runs 数) → 複勝率。runs=0 は None。"""
    def f(feats: dict):
        runs = _num(feats.get(runs_key))
        top3 = _num(feats.get(top3_key))
        if not runs or runs <= 0 or top3 is None:
            return None
        return top3 / runs
    return f


# --- aggregate 特徴の 1 過去走メトリクス ------------------------------------
def _finish(r: dict):
    return _num(r.get("confirmed_order"))


def _is_win(r: dict):
    o = _num(r.get("confirmed_order"))
    return None if o is None else (1.0 if o == 1 else 0.0)


def _is_top3(r: dict):
    o = _num(r.get("confirmed_order"))
    return None if o is None else (1.0 if 1 <= o <= 3 else 0.0)


def _final3f(r: dict):
    v = _num(r.get("final_3f"))
    return None if not v else v          # 0 は未収録


def _past_pop(r: dict):
    return _num(r.get("win_popularity"))


# --- 派生集計メトリクス (DB に専用カラムが無いため導出する) -------------------
# 設計書 §4 STEP2 の「着差 / タイム指数」は keiba.db に専用カラムが存在しない。
# ただし finish_time と final_3f は 100% 充填されているので、そこから導出できる。
# margin_to_winner / final3_rank は race 単位の他馬情報が必要なので
# _enrich_past_runs() が事前に埋める (1 頭 1 クエリに集約)。
def _margin(r: dict):
    """着差: 勝ち馬との走破タイム差 (1/10秒)。0=勝ち馬。小さいほど良い。"""
    return _num(r.get("_margin_to_winner"))


def _time_index(r: dict):
    """タイム指数: 100m あたり走破タイム (1/10秒)。距離差を正規化。小さいほど良い。"""
    ft = _num(r.get("finish_time"))
    d = _num(r.get("distance"))
    if not ft or not d or d <= 0:
        return None
    return ft / d * 100.0


def _final3f_rank(r: dict):
    """上がり3F順位 (race 内)。小さいほど良い。"""
    return _num(r.get("_final3_rank"))


def _final3f_rank_ratio(r: dict):
    """上がり3F順位 ÷ 出走頭数。頭数差を正規化。小さいほど良い。"""
    rk = _num(r.get("_final3_rank"))
    n = _num(r.get("_final3_n"))
    if not rk or not n or n <= 0:
        return None
    return rk / n


# --- コーナー通過順位ベースの派生メトリクス --------------------------------
# keiba.db の corner_order_* は全ゼロ (有効率0%) なので、生 RA レコードから復元した
# 索引 (builder/corner.py) を使う。索引のカバーは 2025 年以降 (生ファイルの範囲)。
# 「第1コーナー」は実際には**最初に記録されたコーナー** (短距離戦は3・4角のみ記録)。
def _corner_first(r: dict):
    return _num(r.get("_corner_first"))


def _corner_last(r: dict):
    return _num(r.get("_corner_last"))


def _gain_first_to_last(r: dict):
    """道中の押し上げ: 最初のコーナー順位 - 最終コーナー順位。大きいほど良い。"""
    return _num(r.get("_corner_gain_first_last"))


def _gain_first_to_finish(r: dict):
    """最初のコーナーから着順までの押し上げ。大きいほど良い。"""
    f, o = _num(r.get("_corner_first")), _num(r.get("confirmed_order"))
    return None if (f is None or not o) else f - o


def _gain_last_to_finish(r: dict):
    """最終コーナーから着順までの押し上げ (末脚)。大きいほど良い。"""
    lv, o = _num(r.get("_corner_last")), _num(r.get("confirmed_order"))
    return None if (lv is None or not o) else lv - o


_CORNER_INDEX: dict | None = None


def corner_index() -> dict:
    """コーナー通過順位索引を遅延ロード (無ければ空 dict で誠実に劣化)。"""
    global _CORNER_INDEX
    if _CORNER_INDEX is None:
        from . import corner as _c
        _CORNER_INDEX = _c.load_corner_index(config.CORNER_INDEX_PATH)
    return _CORNER_INDEX


_RACE_KEY_COLS = ("race_year", "race_month_day", "track_code", "kaiji", "nichiji", "race_num")


def _race_key(r: dict) -> str | None:
    parts = [r.get(c) for c in _RACE_KEY_COLS]
    if any(p is None for p in parts):
        return None
    return "".join(str(p) for p in parts)


def _enrich_past_runs(conn, runs: list[dict], cache: dict) -> None:
    """過去走に派生値 (_margin_to_winner, _final3_rank, _final3_n) を埋める。

    レース単位の他馬情報が必要なため、未キャッシュのレースをまとめて 1 クエリで取得する
    (過去走ごとに問い合わせると 1 頭で数十クエリになり実用速度にならない)。read-only。
    """
    need: list[str] = []
    for r in runs:
        k = _race_key(r)
        if k and k not in cache:
            need.append(k)
    need = list(dict.fromkeys(need))
    if need:
        # ★連結キーの IN は index を使えず horse_races 全スキャンになる (実測で 10 倍遅化)。
        # 主キー (race_year, race_month_day, track_code, kaiji, nichiji, race_num, horse_num)
        # を使えるよう、列ごとの等値比較を OR で並べる。
        keys = [(k[0:4], k[4:8], k[8:10], k[10:12], k[12:14], k[14:16]) for k in need]
        cond = " OR ".join(
            ["(race_year=? AND race_month_day=? AND track_code=? AND kaiji=? "
             "AND nichiji=? AND race_num=?)"] * len(keys))
        params = [v for t in keys for v in t]
        rows = conn.execute(
            f"""SELECT race_year, race_month_day, track_code, kaiji, nichiji, race_num,
                       horse_num, finish_time, final_3f
                  FROM horse_races
                 WHERE ({cond}) AND confirmed_order > 0""",
            params,
        ).fetchall()
        grouped: dict[str, list] = {k: [] for k in need}
        for row in rows:
            rk = (f"{row['race_year']}{row['race_month_day']}{row['track_code']}"
                  f"{row['kaiji']}{row['nichiji']}{row['race_num']}")
            grouped.setdefault(rk, []).append(row)
        for k, rs in grouped.items():
            times = [_num(x["finish_time"]) for x in rs]
            times = [t for t in times if t]
            win_time = min(times) if times else None
            f3 = [(str(x["horse_num"]), _num(x["final_3f"])) for x in rs if _num(x["final_3f"])]
            f3.sort(key=lambda t: t[1])
            rank = {num: i for i, (num, _v) in enumerate(f3, start=1)}
            cache[k] = {"win_time": win_time, "f3_rank": rank, "f3_n": len(f3)}

    cidx = corner_index()
    for r in runs:
        k = _race_key(r)
        info = cache.get(k) if k else None
        if info:
            ft = _num(r.get("finish_time"))
            wt = info["win_time"]
            r["_margin_to_winner"] = (ft - wt) if (ft and wt is not None) else None
            r["_final3_rank"] = info["f3_rank"].get(str(r.get("horse_num")))
            r["_final3_n"] = info["f3_n"] or None
        if k and cidx:
            from . import corner as _c
            hn = str(int(r["horse_num"])) if str(r.get("horse_num", "")).strip().isdigit() else None
            if hn:
                cp = _c.corner_positions(cidx, k, hn)
                r["_corner_first"] = cp["first"]
                r["_corner_last"] = cp["last"]
                r["_corner_gain_first_last"] = cp["gain_first_last"]


# --- current 特徴のメトリクス -----------------------------------------------
def _cur(key: str) -> Callable[[dict], float | None]:
    return lambda h: _num(h.get(key))


def _F(key, label, category, kind, hib, metric) -> Feature:
    return Feature(key, label, category, kind, hib, metric)


_FEATURE_LIST: list[Feature] = [
    # ===== compute: 成績 =====
    _F("recent_avg_finish", "平均着順", "成績", "compute", False, _cf("recent_avg_finish")),
    _F("recent_avg_finish_rate", "着順率(着順/頭数)", "成績", "compute", False, _cf("recent_avg_finish_rate")),
    _F("recent_best_finish", "最高着順", "成績", "compute", False, _cf("recent_best_finish")),
    _F("recent_win_count", "直近勝利数", "成績", "compute", True, _cf("recent_win_count")),
    _F("recent_top3_count", "直近複勝数", "成績", "compute", True, _cf("recent_top3_count")),
    _F("last_finish", "前走着順", "成績", "compute", False, _cf("last_finish")),
    _F("recent_trend_delta", "調子トレンド", "成績", "compute", True, _cf("recent_trend_delta")),
    # ===== compute: 上がり・タイム =====
    _F("avg_final_3f", "平均上がり3F", "上がり・タイム", "compute", False, _cf("avg_final_3f")),
    _F("best_final_3f", "最速上がり3F", "上がり・タイム", "compute", False, _cf("best_final_3f")),
    _F("best_final_3f_rank", "上がり最高順位", "上がり・タイム", "compute", False, _cf("best_final_3f_rank")),
    _F("best_time_per_100m", "100mあたり最速", "上がり・タイム", "compute", False, _cf("best_time_per_100m")),
    _F("best_relative_time_diff", "相対タイム差", "上がり・タイム", "compute", True, _cf("best_relative_time_diff")),
    # ===== compute: コーナー =====
    _F("recent_4corner_avg_position", "4角平均位置", "展開", "compute", False, _cf("recent_4corner_avg_position")),
    _F("recent_4corner_position_change", "コーナーでの押し上げ", "展開", "compute", True, _cf("recent_4corner_position_change")),
    _F("draw_position", "枠位置(内→外)", "展開", "compute", False, _cf("draw_position")),
    # ===== compute: 騎手 =====
    _F("jockey_win_rate", "騎手勝率", "騎手", "compute", True, _cf("jockey_win_rate")),
    _F("jockey_recent_30d_top3_rate", "騎手30日複勝率", "騎手", "compute", True, _cf("jockey_recent_30d_top3_rate")),
    _F("jockey_recent_90d_top3_rate", "騎手90日複勝率", "騎手", "compute", True, _cf("jockey_recent_90d_top3_rate")),
    _F("jockey_track_top3_rate", "騎手×当競馬場", "騎手", "compute", True, _cf("jockey_track_top3_rate")),
    # ===== compute: 調教師 =====
    _F("trainer_win_rate", "調教師勝率", "調教師", "compute", True, _cf("trainer_win_rate")),
    _F("trainer_recent_30d_top3_rate", "調教師30日複勝率", "調教師", "compute", True, _cf("trainer_recent_30d_top3_rate")),
    _F("trainer_track_top3_rate", "調教師×当競馬場", "調教師", "compute", True, _cf("trainer_track_top3_rate")),
    # ===== compute: 血統 =====
    _F("sire_surface_top3_rate", "父×芝ダート", "血統", "compute", True, _cf("sire_surface_top3_rate")),
    _F("sire_going_top3_rate", "父×馬場状態", "血統", "compute", True, _cf("sire_going_top3_rate")),
    _F("sire_distance_top3_rate", "父×距離", "血統", "compute", True, _cf("sire_distance_top3_rate")),
    _F("sire_track_top3_rate", "父×競馬場", "血統", "compute", True, _cf("sire_track_top3_rate")),
    _F("dam_sire_surface_top3_rate", "母父×芝ダート", "血統", "compute", True, _cf("dam_sire_surface_top3_rate")),
    _F("dam_sire_going_top3_rate", "母父×馬場状態", "血統", "compute", True, _cf("dam_sire_going_top3_rate")),
    _F("dam_sire_distance_top3_rate", "母父×距離", "血統", "compute", True, _cf("dam_sire_distance_top3_rate")),
    # ===== compute: コース・条件適性 (count ペアから複勝率を導出) =====
    _F("fit_course", "同コース複勝率", "適性", "compute", True, _cf_rate("same_course_top3", "same_course_runs")),
    _F("fit_course_distance", "同コース同距離複勝率", "適性", "compute", True, _cf_rate("same_course_distance_top3", "same_course_distance_runs")),
    _F("fit_distance", "同距離複勝率", "適性", "compute", True, _cf_rate("same_distance_top3", "same_distance_runs")),
    _F("fit_going", "同馬場状態複勝率", "適性", "compute", True, _cf_rate("same_going_top3", "same_going_runs")),
    _F("fit_surface", "同芝ダート複勝率", "適性", "compute", True, _cf_rate("same_track_type_top3", "same_track_type_runs")),
    _F("fit_bucket", "同距離帯複勝率", "適性", "compute", True, _cf_rate("same_bucket_top3", "same_bucket_runs")),
    _F("horse_track_top3_rate", "当競馬場での自身複勝率", "適性", "compute", True, _cf("horse_track_top3_rate")),
    _F("horse_recent_90d_top3_rate", "直近90日複勝率", "適性", "compute", True, _cf("horse_recent_90d_top3_rate")),
    # ===== compute: 状態 =====
    _F("days_since_last", "出走間隔(日)", "状態", "compute", False, _cf("days_since_last")),
    _F("burden_delta", "斤量変化", "状態", "compute", False, _cf("burden_delta")),
    # ===== current: 現在レース属性 =====
    _F("popularity", "人気(市場)", "市場", "current", False, _cur("win_popularity")),
    _F("burden_weight", "斤量", "状態", "current", True, _cur("burden_weight")),
    _F("horse_weight_change", "馬体重変化", "状態", "current", True, _cur("weight_change_diff")),
    # ===== aggregate: 自前の可変集計 (直近N走 × 一致条件) =====
    _F("agg_avg_finish", "平均着順(可変集計)", "可変集計", "aggregate", False, _finish),
    _F("agg_win_rate", "勝率(可変集計)", "可変集計", "aggregate", True, _is_win),
    _F("agg_top3_rate", "複勝率(可変集計)", "可変集計", "aggregate", True, _is_top3),
    _F("agg_avg_final3f", "平均上がり3F(可変集計)", "可変集計", "aggregate", False, _final3f),
    _F("agg_avg_popularity", "平均人気(可変集計)", "可変集計", "aggregate", False, _past_pop),
    # 設計書 §4 STEP2 で必要だが DB に専用カラムが無く、導出で補った項目
    _F("agg_margin", "着差(可変集計)", "可変集計", "aggregate", False, _margin),
    _F("agg_time_index", "タイム指数(可変集計)", "可変集計", "aggregate", False, _time_index),
    _F("agg_final3f_rank", "上がり3F順位(可変集計)", "可変集計", "aggregate", False, _final3f_rank),
    _F("agg_final3f_rank_ratio", "上がり3F順位率(可変集計)", "可変集計", "aggregate", False,
       _final3f_rank_ratio),
    # コーナー通過順位系 (生 RA から復元。索引が無い期間は None で誠実に劣化)
    _F("agg_corner_first", "第1コーナー通過順位(可変集計)", "可変集計", "aggregate", False,
       _corner_first),
    _F("agg_corner_last", "最終コーナー通過順位(可変集計)", "可変集計", "aggregate", False,
       _corner_last),
    _F("agg_gain_first_to_last", "第1→最終コーナーの着順上昇(可変集計)", "可変集計",
       "aggregate", True, _gain_first_to_last),
    _F("agg_gain_first_to_finish", "第1コーナーからの着順上昇(可変集計)", "可変集計",
       "aggregate", True, _gain_first_to_finish),
    _F("agg_gain_last_to_finish", "最終コーナーからの着順上昇(可変集計)", "可変集計",
       "aggregate", True, _gain_last_to_finish),
]

FEATURES: dict[str, Feature] = {f.key: f for f in _FEATURE_LIST}


def list_features() -> list[dict]:
    """UI 用: 特徴量メタ (キー/日本語名/カテゴリ/種別) の一覧。"""
    return [{"key": f.key, "label": f.label, "category": f.category,
             "kind": f.kind, "higher_is_better": f.higher_is_better}
            for f in _FEATURE_LIST]


# ---------------------------------------------------------------------------
# 特徴量の計算
# ---------------------------------------------------------------------------
def _run_matches(run: dict, race: dict, match: list[str]) -> bool:
    if "surface" in match and _surface(run.get("track_type_code")) != _surface(race.get("track_type_code")):
        return False
    if "track" in match and str(run.get("track_code")) != str(race.get("track_code")):
        return False
    if "distance" in match and _num(run.get("distance")) != _num(race.get("distance")):
        return False
    return True


def _aggregate_value(feat: Feature, past: list[dict], race: dict,
                     match: list[str], lookback: int | None) -> float | None:
    runs = [r for r in past if _run_matches(r, race, match or [])]
    if lookback:
        runs = runs[:lookback]
    vals = [v for v in (feat.metric(r) for r in runs) if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _past_runs(conn, brn: str, before: str, limit: int = 50,
               cache: dict | None = None) -> list[dict]:
    """過去走を取得し、派生値 (着差・上がり3F順位) を埋めて返す。

    cache を渡すとレース単位の他馬情報を再利用する (行列構築で必須)。
    """
    _ensure_keiba_on_path()
    from predictor.features import horse_past_runs  # type: ignore
    runs = horse_past_runs(conn, brn, before, limit=limit)
    if runs:
        rc = cache.setdefault("_race_ctx", {}) if cache is not None else {}
        _enrich_past_runs(conn, runs, rc)
    return runs


def _compute_features(conn, horse: dict, race: dict, cache: dict) -> dict:
    _ensure_keiba_on_path()
    from predictor.features import compute_features  # type: ignore
    return compute_features(conn, horse, race, cache=cache)


def compute_feature_rows(conn, horses: list[dict], race: dict, cfg: dict,
                         cache: dict | None = None) -> dict[str, dict[str, float | None]]:
    """各馬について config で選ばれた特徴量の生値を計算。{horse_num: {key: value|None}}。"""
    cache = cache if cache is not None else {}
    before = f"{race.get('race_year')}{race.get('race_month_day')}"
    specs = [s for s in cfg.get("features", []) if s.get("key") in FEATURES]
    kinds = {FEATURES[s["key"]].kind for s in specs}
    need_compute = "compute" in kinds
    need_past = "aggregate" in kinds

    rows: dict[str, dict[str, float | None]] = {}
    for h in horses:
        hn = str(h.get("horse_num"))
        feats: dict = {}
        if need_compute:
            fkey = ("cf", h.get("blood_register_num"), before, hn)
            if fkey not in cache:
                cache[fkey] = _compute_features(conn, h, race, cache)
            feats = cache[fkey]
        past: list[dict] = []
        if need_past:
            brn = h.get("blood_register_num")
            pkey = ("past50", brn, before)
            if pkey not in cache:
                cache[pkey] = _past_runs(conn, brn, before, cache=cache) if brn else []
            past = cache[pkey]

        vals: dict[str, float | None] = {}
        for spec in specs:
            feat = FEATURES[spec["key"]]
            if feat.kind == "compute":
                vals[feat.key] = feat.metric(feats)
            elif feat.kind == "aggregate":
                vals[feat.key] = _aggregate_value(feat, past, race,
                                                  spec.get("match") or [], spec.get("lookback"))
            else:  # current
                vals[feat.key] = feat.metric(h)
        rows[hn] = vals
    return rows


def score_from_features(feature_rows: dict[str, dict[str, float | None]],
                        cfg: dict) -> list[tuple[str, float]]:
    """特徴量の生値 → レース内 z-score 正規化 → 向き調整 → 重み付き和。純粋関数。"""
    horse_nums = list(feature_rows.keys())
    scores = {hn: 0.0 for hn in horse_nums}

    for spec in cfg.get("features", []):
        feat = FEATURES.get(spec.get("key"))
        if feat is None:
            continue
        weight = float(spec.get("weight", 0.0))
        if weight == 0.0:
            continue
        present = {hn: feature_rows.get(hn, {}).get(feat.key) for hn in horse_nums}
        vals = [v for v in present.values() if v is not None]
        if len(vals) < 2:
            continue
        mean = statistics.fmean(vals)
        stdev = statistics.pstdev(vals)
        if stdev == 0:
            continue
        direction = 1.0 if feat.higher_is_better else -1.0
        for hn in horse_nums:
            v = present[hn]
            if v is None:
                continue
            scores[hn] += weight * direction * ((v - mean) / stdev)

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def predict(conn, race: dict, horses: list[dict], cfg: dict,
            cache: dict | None = None) -> list[dict]:
    """1 レースを config で予想。順位付き (rank 1 = ◎) の馬情報リストを返す。"""
    rows = compute_feature_rows(conn, horses, race, cfg, cache)
    ranked = score_from_features(rows, cfg)
    by_num = {str(h.get("horse_num")): h for h in horses}
    out = []
    for rank, (hn, score) in enumerate(ranked, start=1):
        h = by_num.get(hn, {})
        out.append({
            "rank": rank, "horse_num": hn, "score": round(score, 4),
            "confirmed_order": h.get("confirmed_order"),
            "win_popularity": h.get("win_popularity"),
        })
    return out


def default_config() -> dict:
    """動作確認用の既定設定 (幅=compute と 深さ=aggregate を混ぜた例)。"""
    return {"features": [
        {"key": "agg_top3_rate", "weight": 1.0, "lookback": 5, "match": ["surface"]},
        {"key": "recent_avg_finish", "weight": 1.0},
        {"key": "jockey_win_rate", "weight": 0.7},
        {"key": "fit_distance", "weight": 0.7},
        {"key": "popularity", "weight": 0.5},
    ]}
