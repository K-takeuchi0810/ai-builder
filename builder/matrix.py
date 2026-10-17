"""Phase 3a: 特徴量行列の事前計算 + ディスクキャッシュ。

compute_features は 1 頭ごとに騎手・血統等を DB 集計するため高コスト。Phase 3 の重み自動探索は
「同じ特徴量を重みだけ変えて何百回も評価」するので、毎回 DB を叩くと非現実的に遅い。

そこで候補特徴量セット (columns) について、期間内の全 (レース×馬) の特徴量ベクトル・着順・
単勝払戻・セグメント属性を **一度だけ** 計算して JSON にキャッシュする。以降の探索
(score_race_columns / evaluate_matrix) は行列上の純粋計算だけで回るため高速。

read-only: keiba-yosou は model 経由 (compute_features / horse_past_runs) でのみ参照。
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import axes as ax
from . import config, model
from . import normalize as nrm
from .keiba_bridge import open_conn, _ensure_keiba_on_path

# v2: per-horse に decimal odds(win_odds/10) と win_popularity を追加 (バリューベット EV 用)。
MATRIX_VERSION = 2
_CACHE_DIR = Path(config.__file__).resolve().parent.parent / "out" / "matrix"
logger = logging.getLogger(__name__)
_JST = ZoneInfo("Asia/Tokyo")


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


def _today_jst() -> str:
    """JST の今日 (YYYYMMDD)。キャッシュの「完了した月か」の判定に使う。テストで差し替える。"""
    return datetime.now(_JST).strftime("%Y%m%d")


def _month_end(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{ym}{calendar.monthrange(y, m)[1]:02d}"


def _mtime_jst(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, _JST).strftime("%Y%m%d")


def _cache_usable(path: Path, data: dict, through: str, today: str) -> bool:
    """キャッシュを再利用してよいか。through はキャッシュが覆う最後の日 (月なら月末、範囲なら to_date)。

    2026-10-16 まで月のキャッシュは「作った時点で途中・未来の月」でも永久に再利用していた。7 月に作った
    2026-08〜12 の月が空 (32 バイト) のまま残り、共有サービスの「これまでの成績」が 2026-07-25 で止まって
    7/26〜10/16 の JRA 768 レースが抜けていた。
    - complete (覆う期間がすべて過ぎてから作った) なら再利用する
    - 途中で作ったもの (complete=False) は、作った当日 (built_on == today) だけ再利用する
    - 印の無い古いキャッシュは、ファイルの更新日が through より後 (期間が終わってから作った) なら完了とみなす
    """
    if "complete" in data:
        return bool(data["complete"]) or data.get("built_on") == today
    return _mtime_jst(path) > through


def _stamp(through: str, today: str) -> dict:
    return {"complete": through < today, "built_on": today,
            "built_at": datetime.now(_JST).isoformat(timespec="seconds")}


def _built_at(path: Path, data: dict) -> str:
    """キャッシュを作った時刻 (JST の ISO)。built_at の無い古いキャッシュはファイルの更新時刻。"""
    return data.get("built_at") or datetime.fromtimestamp(path.stat().st_mtime, _JST).isoformat(timespec="seconds")


class RevisionLedgerError(RuntimeError):
    """改訂の台帳が在るのに読めない (strict=True のとき)。"""


def _revisions_path() -> Path:
    return config.KEIBA_DB_PATH.parent / "data_revisions.json"


def _load_revisions(strict: bool = False) -> list[dict]:
    """keiba-yosou の「データの改訂の台帳」(KEIBA_DB_PATH の隣の data_revisions.json)。

    keiba-yosou が過去のデータをまとめて直したとき (血統の表の作り直し・通過順位の backfill など) に
    {"at": JST の ISO, "effective_from": YYYYMMDD, ...} を追記する (keiba-yosou の data_revisions.py)。
    本番の DB はライブ取り込みで 1 分ごとに書き換わるので、DB の更新時刻ではこれは分からない。
    台帳が在るのに読めないとき: strict なら RevisionLedgerError (builder.rebuild_matrix が「作り直す月: 0」で
    黙って終わらないように)。そうでなければ改訂なしとして続け、エラーを残す (サービスの起動は止めない)。
    """
    path = _revisions_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"配列でない: {type(data).__name__}")
    except (OSError, ValueError) as exc:
        if strict:
            raise RevisionLedgerError(f"改訂の台帳 {path} を読めない: {exc}") from exc
        logger.error("改訂の台帳 %s を読めない (改訂は無いものとして続ける。古い月を見逃す)", path, exc_info=True)
        return []
    return [r for r in data if isinstance(r, dict) and r.get("at") and r.get("effective_from")]


def _as_dt(stamp: str) -> datetime:
    """ISO の時刻を JST の aware な datetime に (オフセットの無いものは JST とみなす)。文字列のまま比べると
    +09:00 と +00:00 が混ざったときに最大 9 時間ずれて静かに誤判定する。"""
    dt = datetime.fromisoformat(str(stamp))
    return dt.replace(tzinfo=_JST) if dt.tzinfo is None else dt


def _revisions_after(built_at: str, through: str, revisions: list[dict]) -> list[dict]:
    """キャッシュを作った後に記録され、キャッシュが覆う期間 (〜through) に効く改訂。

    effective_from は「この日以降のレースの特徴量が変わりうる最初の日」。過去の成績を集計する特徴量があるので、
    その日より後のすべての月が影響を受ける (through >= effective_from なら古い)。時刻の読めない改訂は古い扱い
    (作り直す側に倒す)。
    """
    built = _as_dt(built_at)
    out = []
    for r in revisions:
        if str(r["effective_from"]) > through:
            continue
        try:
            if _as_dt(r["at"]) > built:
                out.append(r)
        except ValueError:
            logger.warning("改訂の台帳の時刻が読めない (古い扱いにする): %r", r.get("at"))
            out.append(r)
    return out


REBUILD_LOCK_NAME = "rebuild.lock"


def rebuild_lock_path() -> Path:
    """builder.rebuild_matrix が作り直しの間だけ置く印。サービスは起動時にこれを見て警告する。"""
    return _CACHE_DIR / REBUILD_LOCK_NAME


def _month_cache_path(ym: str, chash: str) -> Path:
    return _CACHE_DIR / "months" / f"m_{ym}_{chash}.json"


def month_cache_status(ym: str, chash: str, today: str, revisions: list[dict]) -> tuple[Path, dict | None, str]:
    """月のキャッシュの状態: ("ok" | "missing" | "incomplete" | "stale")。build_matrix と builder.rebuild_matrix が
    同じ判定を使う (月のファイルの場所と「古い」の規則を 1 か所に)。"""
    mp = _month_cache_path(ym, chash)
    if not mp.exists():
        return mp, None, "missing"
    data = json.loads(mp.read_text(encoding="utf-8"))
    through = _month_end(ym)
    if not _cache_usable(mp, data, through, today):
        return mp, data, "incomplete"
    if _revisions_after(_built_at(mp, data), through, revisions):
        return mp, data, "stale"
    return mp, data, "ok"


def _write_json_atomic(path: Path, data: dict) -> None:
    """プロセス固有の tmp に書いてから置き換える (同じ tmp 名を 2 つのプロセスが書くと壊れたファイルが残る。
    2026-10-16 にサービスと手動の作り直しが同じ月を同時に作りかけた)。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):           # Windows は読み手が開いている間の置き換えが PermissionError になる
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2 * (attempt + 1))


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
                 rebuild: bool = False, stale_data: str = "rebuild") -> dict:
    """候補特徴量 specs について特徴量行列を構築。**月次チェックポイント**で中断に強い。

    - 完成済みの範囲キャッシュ (out/matrix/matrix_*.json) があればそれを読む (高速・後方互換)。
    - 無ければ **月 (YYYYMM) 単位で構築し、各月を out/matrix/months/ に逐次保存**。途中でクラッシュ
      しても次回は未完の月から再開でき、最大でも「その月ぶん (~15分)」しか失わない。
    - 全月そろったら範囲キャッシュを書き出して返す。
    - 範囲・月のキャッシュは、覆う期間が終わる前に作ったものを作った当日しか再利用しない (_cache_usable)。
      途中の月・未来の月を空や途中のまま永久に使い続けないため。
    - keiba-yosou の改訂の台帳 (_load_revisions) に、キャッシュを作った後でその期間に効く改訂があれば古い。
      stale_data="rebuild" (既定) なら作り直す。"reuse" なら使い続けて、戻りの "stale_by_revision" に月を並べ、
      警告を残す (共有サービスの起動用: 改訂は全期間に効くことが多く、起動時に作り直すと数時間止まる。
      作り直しは builder.rebuild_matrix で夜間に行う)。

    戻り: {"from","to","columns","races":[{date, seg, horses:[{num,order,odds,pop,x}],
           tan:{num:yen}, trusted}]}
    """
    if stale_data not in ("rebuild", "reuse"):
        raise ValueError(f"stale_data は rebuild か reuse: {stale_data!r}")
    cols = _columns(specs)
    today = _today_jst()
    revisions = _load_revisions()
    range_path = _cache_path(from_date, to_date, cols)
    if range_path.exists() and not rebuild:
        cached = json.loads(range_path.read_text(encoding="utf-8"))
        if _cache_usable(range_path, cached, to_date, today):
            stale = _revisions_after(_built_at(range_path, cached), to_date, revisions)
            if not stale or stale_data == "reuse":
                if stale:
                    logger.warning("行列 %s〜%s は改訂 %d 件より前に作ったまま使う (作り直しは builder.rebuild_matrix)",
                                   from_date, to_date, len(stale))
                    first = min(str(r["effective_from"]) for r in stale)
                    months = [ym for ym in _months(from_date, to_date) if _month_end(ym) >= first]
                    cached["stale_by_revision"] = sorted(set(cached.get("stale_by_revision") or []) | set(months))
                return cached

    _ensure_keiba_on_path()
    from scripts.backtest import popularity_config  # type: ignore
    max_age = popularity_config().get("max_snapshot_age_min")
    need_compute = any(c["kind"] == "compute" for c in cols)
    need_past = any(c["kind"] == "aggregate" for c in cols)
    chash = _col_hash(cols)

    all_races: list[dict] = []
    stale_kept: list[str] = []
    built_ats: list[str] = []          # 範囲の built_at は組み立てた月の最も古いもの (古い月を含む範囲を新しく見せない)
    (_CACHE_DIR / "months").mkdir(parents=True, exist_ok=True)
    with open_conn() as conn:
        for ym in _months(from_date, to_date):
            mp, cached, status = month_cache_status(ym, chash, today, revisions)
            if not rebuild and (status == "ok" or (status == "stale" and stale_data == "reuse")):
                if status == "stale":
                    stale_kept.append(ym)
                built_ats.append(_built_at(mp, cached))
                all_races.extend(cached["races"])
                continue
            # 実在する月末まで。"YYYYMM31" は 30 日の月で実在しない日付になり、keiba-yosou の
            # guard_analysis_window が ValueError で止める (2026-10-16、9 月の作り直しで発覚)
            lo, hi = f"{ym}01", _month_end(ym)
            races = _races_for_range(conn, lo, hi, cols, need_compute, need_past, max_age)
            stamp = _stamp(_month_end(ym), today)
            _write_json_atomic(mp, {"month": ym, "races": races, **stamp})
            built_ats.append(stamp["built_at"])
            all_races.extend(races)

    # 月キャッシュはフル月ぶん保存するが、返却は要求範囲 [from,to] にクリップする。
    races_out = [r for r in all_races if from_date <= r["date"] <= to_date]
    matrix = {"from": from_date, "to": to_date, "columns": cols, "races": races_out,
              **_stamp(to_date, today)}
    if built_ats:
        matrix["built_at"] = min(built_ats, key=_as_dt)
    if stale_kept:
        matrix["stale_by_revision"] = stale_kept
        logger.warning("改訂より前に作った月 %d 個 (%s〜%s) をそのまま使った。作り直しは "
                       "python -m builder.rebuild_matrix", len(stale_kept), stale_kept[0], stale_kept[-1])
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(range_path, matrix)
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
        # AV速報で取消・除外になった馬は正規化母集団にもスコアにも入れない。
        # フラグを残したまま除外するため、変更撤回時は次回refreshで復帰できる。
        hrows = [hr for hr in r["horses"] if not hr.get("scratched")]
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
