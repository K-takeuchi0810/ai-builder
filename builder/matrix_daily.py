"""設計書 §5 バッチ層: 当日レースの基底列を日次計算する (matrix_daily)。

既存 `builder/matrix.py` は **確定済みレース** (結果あり) を前提にした研究用。
当日運用では結果がまだ無いので、出馬表 (horse_races の登録行) から基底列を作る。

不変条件:
- **PIT**: 過去走は対象レース日より厳密に前だけ (`model._past_runs` が `<` で実装)。
  対象レース自身・同日レースの結果は構造的に混入しない (`tests/test_pit.py` が固定)。
- 欠損ポリシーは適用しない (ここは生値の計算まで)。z 化とゲートは
  `matrix.prepare_races()` / `model.score_race_detailed()` 側で一元的に行う。
- keiba.db は read-only。生 JV-Data も read-only。

出力は `matrix.py` と同じ形 ({"columns", "races":[{date, seg, horses:[{num,order,odds,pop,x}], ...}]})
なので、`prepare_races` / `score_race_detailed` / `presets.gate_check` をそのまま使える。
"""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import time

from . import config, matrix as mx, model, payouts as payout_data
from .keiba_bridge import _ensure_keiba_on_path, open_conn

logger = logging.getLogger(__name__)

# v2: UI が必要とする start_time / 確定状態を各レースに持たせた。
# v3: 枠番 (waku) を各馬に持たせた。UI が馬番から計算していたため誤った枠色が
#     出ていた (7頭立てで 6/7 件外れる)。枠割は頭数依存なので UI 導出は不可能。
# v4: レースのクラス/名称 (race_class / race_title) と、各馬の出走情報
#     (騎手・斤量・調教師・性齢・馬体重と増減) を持たせた。
#     「騎手の成績」を根拠に印を打ちながら騎手名を出していなかったため。
DAILY_VERSION = 6


def _daily_dir() -> Path:
    return Path(config.CORNER_INDEX_PATH).parent / "daily"


def _daily_path(date: str, cols: list[dict]) -> Path:
    # version を名前に含め、形が変わったら旧キャッシュを自動的に使わない
    return _daily_dir() / f"daily_v{DAILY_VERSION}_{date}_{mx._col_hash(cols)}.json"


def build_daily(date: str, specs: list[dict], *, rebuild: bool = False,
                require_confirmed: bool = False) -> dict:
    """当日 (date=YYYYMMDD) の全レースの基底列を計算する。

    require_confirmed=False (既定) なので、結果が出ていない当日レースも対象になる。
    確定後に同じ日を再構築すれば order が埋まる (rebuild=True)。
    """
    cols = mx._columns(specs)
    path = _daily_path(date, cols)
    if path.exists() and not rebuild:
        return json.loads(path.read_text(encoding="utf-8"))

    _ensure_keiba_on_path()
    from scripts.backtest import (  # type: ignore
        get_payout_row, horses_for_race, list_races, popularity_config, race_odds_untrusted,
    )
    max_age = popularity_config().get("max_snapshot_age_min")
    need_compute = any(c["kind"] == "compute" for c in cols)
    need_past = any(c["kind"] == "aggregate" for c in cols)

    races_out: list[dict] = []
    with open_conn() as conn:
        cache: dict = {}
        races = list_races(conn, date, date, jra_only=True,
                           require_confirmed=require_confirmed)
        for race in races:
            horses = horses_for_race(conn, race)
            if not horses:
                continue
            live_race = _with_latest_weather(conn, _with_latest_race_changes(conn, race))
            inactive = _inactive_horses(conn, race)
            before = f"{race.get('race_year')}{race.get('race_month_day')}"
            hrows = []
            for h in horses:
                feats = {}
                if need_compute:
                    fkey = ("cf", h.get("blood_register_num"), before, str(h.get("horse_num")))
                    if fkey not in cache:
                        cache[fkey] = model._compute_features(conn, h, live_race, cache)
                    feats = cache[fkey]
                past = []
                if need_past:
                    brn = h.get("blood_register_num")
                    pkey = ("past50", brn, before)
                    if pkey not in cache:
                        # PIT: before より厳密に前のみ (対象レース・同日レースは入らない)
                        cache[pkey] = model._past_runs(conn, brn, before, cache=cache) if brn else []
                    past = cache[pkey]
                x: dict[str, float | None] = {}
                for c in cols:
                    feat = model.FEATURES[c["key"]]
                    if c["kind"] == "compute":
                        x[c["id"]] = feat.metric(feats)
                    elif c["kind"] == "aggregate":
                        x[c["id"]] = model._aggregate_value(feat, past, live_race,
                                                            c["match"], c["lookback"])
                    else:
                        x[c["id"]] = feat.metric(h)
                wo = model._num(h.get("win_odds"))
                inactive_info = inactive.get(str(h.get("horse_num") or "").zfill(2))
                hrows.append({
                    "num": str(h.get("horse_num")),
                    # 枠番は DB の値をそのまま持つ。**馬番から計算してはいけない** —
                    # JRA の枠割は頭数依存で、7頭立てでは馬番=枠番になる
                    # (ceil(馬番/2) 式は実測で 6/7 件外れた)。
                    "waku": _waku(h),
                    "order": h.get("confirmed_order"),          # 当日は 0/None
                    "odds": (wo / 10.0) if (wo and wo > 0) else None,
                    "pop": model._num(h.get("win_popularity")),
                    "name": (h.get("horse_name") or "").strip(),
                    "n_past_runs": len(past),                   # カバレッジ表示用
                    # 出走情報 (すべて発走前に確定する情報)。
                    # 馬体重だけは発表前は None になる — PIT を迂回しない。
                    **_entry_info(h),
                    "scratched": bool(inactive_info),
                    "scratch_status": inactive_info.get("label") if inactive_info else None,
                    "x": x,
                })
            seg = mx._seg(live_race)
            race_out = {
                "race_id": _race_id(race),
                "date": before,
                "race_num": str(race.get("race_num")),
                # 名称とクラスを **別のスロット** に持つ。表示名スロットに条件を
                # 焼き込むと、実名を持つ特別戦で名前が条件を上書きして芝/ダート・
                # 距離が画面から消える。UI は 1行目=名称かクラス、2行目=条件 に分ける。
                "race_title": _race_title(live_race),
                "race_class": _race_class(live_race),
                "race_name": _display_name(live_race, seg),   # 後方互換 (旧UI用)
                "start_time": _hhmm(live_race.get("start_time")),   # UI の発走時刻表示用
                "seg": seg,
                **_change_metadata(live_race),
                "horses": hrows,
                "tan": mx._tan_payouts(get_payout_row(conn, race)),
                "trusted": not race_odds_untrusted(horses, live_race, max_age),
                "odds_as_of": _odds_as_of(horses),           # UI はオッズに取得時刻を添える
                "weight_announced": _weight_announced(horses),
                "live_feature_context": _feature_context(seg, horses),
            }
            race_out["payouts"] = payout_data.for_race(race_out)
            races_out.append(race_out)

    for race in races_out:
        race["live_revision"] = _live_revision(race)
        race["live_updated_at"] = None
    daily = {"version": DAILY_VERSION, "date": date, "from": date, "to": date,
             "columns": cols, "races": races_out, "live_updated_at": None}
    _daily_dir().mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(daily, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return daily


def _hhmm(raw) -> str | None:
    """start_time ("1545") → "15:45"。欠損は None。"""
    s = str(raw or "").strip()
    return f"{s[:2]}:{s[2:4]}" if len(s) >= 4 and s[:4].isdigit() else None


def _display_name(race: dict, seg: dict) -> str:
    """表示用のレース名。通常レースは race_name が空なので条件から組み立てる。

    実データ確認: 平場は race_name / race_short10 / race_short6 すべて空
    (重賞のみ命名される)。UI に空文字を出さないためのフォールバック。
    """
    from . import labels as lb
    name = (race.get("race_name") or race.get("race_short10")
            or race.get("race_short6") or "").strip()
    if name:
        return name
    track = lb.value_label("track", seg.get("track"))
    surface = lb.value_label("surface", seg.get("surface"))
    dist = seg.get("distance")
    parts = [p for p in (track, f"{surface}{dist}m" if dist else surface) if p]
    return " ".join(parts) or f"{race.get('race_num')}R"


def _race_id(race: dict) -> str:
    return (f"{race.get('race_year')}{race.get('race_month_day')}"
            f"{race.get('track_code')}{race.get('kaiji')}"
            f"{race.get('nichiji')}{race.get('race_num')}")


def _race_title(race: dict) -> str | None:
    """特別戦の名称。平場は None (クラスと条件で表す)。"""
    name = (race.get("race_name") or race.get("race_short10")
            or race.get("race_short6") or "").strip()
    return name or None


def _race_class(race: dict) -> str | None:
    """クラス表示 (新馬/未勝利/1勝クラス/…/オープン/G1〜G3/リステッド)。

    keiba.db に競走条件コードが無いので生 RA から復元した索引を引く
    (builder/raceclass.py)。索引が無い期間は None で誠実に劣化する。
    """
    from . import raceclass as rc
    got = rc.lookup(race)
    return rc.race_class_label(got.get("class_code"), got.get("grade"))


def _entry_info(h: dict) -> dict:
    """出走情報 (騎手・斤量・調教師・性齢・馬体重と増減)。

    AI が「騎手の最近30日の成績」を根拠に印を打つのに騎手名を一度も出して
    いなかったため、行に出せるだけの情報を持たせる。

    **PIT を迂回しない**: 馬体重は発表されるまで DB が空なので、そのまま
    None を返す (`weight_announced` が False のレースでは UI が「発表待ち」を出す)。
    """
    from . import labels as lb
    bw = model._num(h.get("burden_weight"))
    hw = str(h.get("horse_weight") or "").strip()
    sign = str(h.get("weight_change_sign") or "").strip()
    diff = str(h.get("weight_change_diff") or "").strip()
    change = None
    if diff.isdigit():
        n = int(diff)
        change = -n if sign == "-" else n
    sex = lb.value_label("sex", str(h.get("sex_code") or ""))
    age = model._num(h.get("age"))
    return {
        "jockey": (h.get("jockey_short_name") or "").strip() or None,
        "jockey_code": (h.get("jockey_code") or "").strip() or None,
        # 斤量は 0.1kg 単位で格納されている (555 → 55.5kg)
        "burden_weight": (bw / 10.0) if bw else None,
        "trainer": (h.get("trainer_short_name") or "").strip() or None,
        "sex_age": (f"{sex}{int(age)}" if sex and age else None),
        "horse_weight": int(hw) if hw.isdigit() else None,
        "horse_weight_change": change,
    }


def _inactive_horses(conn, race: dict) -> dict[str, dict]:
    """現在有効なAV速報を馬番で返す。テーブル未導入DBでは安全に空へ戻す。"""
    try:
        rows = conn.execute(
            "SELECT horse_num,data_div,announced_time,horse_name FROM race_cancellations "
            "WHERE race_year=? AND race_month_day=? AND track_code=? AND kaiji=? "
            "AND nichiji=? AND race_num=?",
            (race.get("race_year"), race.get("race_month_day"), race.get("track_code"),
             race.get("kaiji"), race.get("nichiji"), race.get("race_num")),
        ).fetchall()
    except Exception:
        return {}
    labels = {"1": "出走取消", "2": "競走除外"}
    return {
        str(row["horse_num"] or "").zfill(2): {
            "label": labels.get(str(row["data_div"] or ""), "取消・除外"),
            "announced_time": row["announced_time"],
            "horse_name": row["horse_name"],
        }
        for row in rows
    }


def _feature_context_parts(seg: dict, horses: list[dict]) -> dict:
    """当日再計算の入力。**指紋と中身を同じ場所から作る** (ずれないように)。"""
    return {
        "condition": seg.get("condition"),
        "track": seg.get("track"),
        "surface": seg.get("surface"),
        "distance": seg.get("distance"),
        "jockeys": sorted([
            str(h.get("horse_num") or "").zfill(2),
            str(h.get("jockey_code") or ""),
            str(h.get("burden_weight") or ""),
        ] for h in horses),
    }


def _feature_context(seg: dict, horses: list[dict]) -> str:
    """馬場・騎手・負担重量に依存する当日再計算の入力指紋。"""
    raw = json.dumps(_feature_context_parts(seg, horses), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _context_changes(before: dict | None, after: dict) -> list[str]:
    """指紋が変わった原因を、変わった項目名で返す。

    当日再計算は1レースあたり十数秒・約400本のDB問い合わせを要する。
    **何がきっかけで回っているのかが分からないと、頻度も妥当性も判断できない。**
    騎手欄は頭数ではなく「何頭ぶん変わったか」を出す (全件を記録しない)。
    """
    if not before:
        return ["(前回の記録なし)"]
    out = []
    for key in ("condition", "track", "surface", "distance"):
        if before.get(key) != after.get(key):
            out.append(f"{key}:{before.get(key)}→{after.get(key)}")
    old_j = {tuple(x) for x in (before.get("jockeys") or [])}
    new_j = {tuple(x) for x in (after.get("jockeys") or [])}
    moved = len(new_j - old_j)
    if moved:
        out.append(f"jockeys:{moved}頭ぶん")
    return out or ["(項目の差は検出できず)"]


def _waku(horse: dict) -> int | None:
    """枠番 (1〜8)。DB に無ければ None を返し、UI は色を付けない。

    間違った枠色を出すくらいなら出さない (推測式は禁止)。
    """
    raw = str(horse.get("waku_num") or "").strip()
    if not raw.isdigit():
        return None
    n = int(raw)
    return n if 1 <= n <= 8 else None


def _odds_as_of(horses: list[dict]) -> str | None:
    """市場オッズ snapshot の最新取得時刻 (ISO 文字列)。

    UI はオッズに必ず取得時刻を添える (UI指示書 §4)。**クライアントの時計で代用しては
    いけない** ため、DB の `odds_fetched_at` をそのまま返す。全馬 NULL の場合は
    確定オッズ (or 未 mining) なので None を返し、UI は時刻を出さない。
    ISO 8601 なので辞書順の max が時刻順の max と一致する。
    """
    stamps = [str(h.get("odds_fetched_at")) for h in horses if h.get("odds_fetched_at")]
    return max(stamps) if stamps else None


def _weight_announced(horses: list[dict]) -> bool:
    """馬体重が発表済みか (設計書 §3.3 の「分析待ち」判定用)。"""
    return any(str(h.get("horse_weight") or "").strip().isdigit() for h in horses)


def _race_change_rows(conn, table: str, race: dict) -> list[dict]:
    try:
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE race_year=? AND race_month_day=? "
            "AND track_code=? AND kaiji=? AND nichiji=? AND race_num=? "
            "ORDER BY announced_time",
            (race.get("race_year"), race.get("race_month_day"),
             race.get("track_code"), race.get("kaiji"), race.get("nichiji"),
             race.get("race_num")),
        ).fetchall()]
    except Exception:
        return []


def _with_latest_race_changes(conn, race: dict) -> dict:
    """Overlay effective TC/CC values and retain display/audit metadata."""
    from . import labels as lb
    out = dict(race)
    time_rows = _race_change_rows(conn, "start_time_changes", race)
    if time_rows:
        first, latest = time_rows[0], time_rows[-1]
        new_time = str(latest.get("new_start_time") or "")
        if (len(new_time) == 4 and new_time.isdigit()
                and int(new_time[:2]) <= 23 and int(new_time[2:]) <= 59):
            out["start_time"] = new_time
            out["start_time_changed"] = True
            out["original_start_time"] = first.get("old_start_time")
            out["start_time_change_as_of"] = latest.get("announced_time")

    course_rows = _race_change_rows(conn, "course_changes", race)
    if course_rows:
        first, latest = course_rows[0], course_rows[-1]
        new_distance = int(latest.get("new_distance") or 0)
        new_type = str(latest.get("new_track_type_code") or "")
        if new_distance > 0 and len(new_type) == 2 and new_type.isdigit():
            old_race = dict(out)
            old_race["distance"] = first.get("old_distance")
            old_race["track_type_code"] = first.get("old_track_type_code")
            old_seg = mx._seg(old_race)
            out["distance"] = new_distance
            out["track_type_code"] = new_type
            new_seg = mx._seg(out)
            reasons = {"1": "強風", "2": "台風", "3": "積雪", "4": "その他"}
            out["course_changed"] = True
            out["course_change"] = {
                "old_surface": old_seg.get("surface"),
                "old_surface_label": lb.value_label("surface", old_seg.get("surface")),
                "old_distance": first.get("old_distance"),
                "new_surface": new_seg.get("surface"),
                "new_surface_label": lb.value_label("surface", new_seg.get("surface")),
                "new_distance": new_distance,
                "reason_code": latest.get("reason_code"),
                "reason": reasons.get(str(latest.get("reason_code") or ""), "主催者発表"),
                "announced_time": latest.get("announced_time"),
            }
    return out


def _change_metadata(race: dict) -> dict:
    return {
        "start_time_changed": bool(race.get("start_time_changed")),
        "original_start_time": _hhmm(race.get("original_start_time")),
        "start_time_change_as_of": race.get("start_time_change_as_of"),
        "course_changed": bool(race.get("course_changed")),
        "course_change": race.get("course_change"),
    }


def _with_latest_weather(conn, race: dict) -> dict:
    """速報天候馬場 (WE) があればレース行へ重ねる。

    ``races`` の馬場値は出馬表時点のままの場合がある。0B14 は別テーブルへ
    保存されるため、ライブ画面では開催単位の最新発表を明示的に参照する。
    """
    out = dict(race)
    try:
        rows = conn.execute(
            "SELECT weather_code,going_turf,going_dirt,announced_time "
            "FROM weather_going WHERE race_year=? AND race_month_day=? "
            "AND track_code=? AND kaiji=? AND nichiji=? "
            "ORDER BY announced_time DESC",
            (race.get("race_year"), race.get("race_month_day"),
             race.get("track_code"), race.get("kaiji"), race.get("nichiji")),
        ).fetchall()
    except Exception:
        rows = []
    # WE は「天候だけ変更」の行では馬場コードが 0 になる。最新1行だけを見ると、
    # その前に発表済みの良/稍重/重/不良を消して「不明」へ戻してしまう。
    # 項目ごとに最新の非0値を採用し、馬場の時刻も採用した行の時刻を使う。
    weather_set = turf_set = dirt_set = False
    condition_stamps: list[str] = []
    for raw in rows:
        current = dict(raw)
        if not weather_set and str(current.get("weather_code") or "0") != "0":
            out["weather_code"] = current["weather_code"]
            weather_set = True
        stamp = str(current.get("announced_time") or "")
        if not turf_set and str(current.get("going_turf") or "0") != "0":
            out["turf_condition"] = current["going_turf"]
            turf_set = True
            if stamp:
                condition_stamps.append(stamp)
        if not dirt_set and str(current.get("going_dirt") or "0") != "0":
            out["dirt_condition"] = current["going_dirt"]
            dirt_set = True
            if stamp:
                condition_stamps.append(stamp)
        if weather_set and turf_set and dirt_set:
            break
    if condition_stamps:
        out["condition_as_of"] = max(condition_stamps)
    return out


def load_daily(date: str, specs: list[dict]) -> dict:
    p = _daily_path(date, mx._columns(specs))
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def load_historical_daily(date: str, specs: list[dict]) -> dict:
    """Load the newest usable daily cache for a historical result screen.

    ``DAILY_VERSION`` intentionally prevents an old feature matrix from being used
    for a new prediction.  Result and purchase-history screens are different: they
    only need the race identity and are refreshed from the authoritative database
    immediately after loading.  Hiding every previous meeting whenever the cache
    format changes made completed purchases appear to wait for results forever.

    Prefer the current cache.  If it does not exist, accept only a versioned cache
    with the same column hash and the expected date, choosing the newest version.
    Unversioned legacy files remain excluded because their schema is unknown.
    """
    current = load_daily(date, specs)
    if current:
        return current

    columns_hash = mx._col_hash(mx._columns(specs))
    pattern = re.compile(
        rf"^daily_v(?P<version>\d+)_{re.escape(str(date))}_{columns_hash}\.json$"
    )
    candidates: list[tuple[int, Path]] = []
    directory = _daily_dir()
    if not directory.exists():
        return {}
    for path in directory.glob(f"daily_v*_{date}_{columns_hash}.json"):
        match = pattern.match(path.name)
        if not match:
            continue
        version = int(match.group("version"))
        if version < DAILY_VERSION:
            candidates.append((version, path))

    for _version, path in sorted(candidates, reverse=True):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(loaded, dict)
                and str(loaded.get("date") or "") == str(date)
                and isinstance(loaded.get("races"), list)):
            return loaded
    return {}


def _live_revision(race: dict) -> str:
    """Return a stable fingerprint of values that may change on race day."""
    values = []
    for horse in race.get("horses", []):
        values.append([
            horse.get("num"), horse.get("order"), horse.get("odds"), horse.get("pop"),
            horse.get("waku"), horse.get("jockey"), horse.get("burden_weight"),
            horse.get("jockey_code"), horse.get("scratched"), horse.get("scratch_status"),
            horse.get("trainer"), horse.get("sex_age"), horse.get("horse_weight"),
            horse.get("horse_weight_change"),
        ])
    payload = {
        "horses": values,
        "odds_as_of": race.get("odds_as_of"),
        "weight_announced": race.get("weight_announced"),
        "trusted": race.get("trusted"),
        "seg": race.get("seg"),
        "start_time": race.get("start_time"),
        "start_time_changed": race.get("start_time_changed"),
        "original_start_time": race.get("original_start_time"),
        "course_changed": race.get("course_changed"),
        "course_change": race.get("course_change"),
        "live_feature_context": race.get("live_feature_context"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def refresh_live(daily: dict, *, recompute_features: bool = True) -> dict:
    """Merge current DB values into an already-built daily feature matrix.

    Historical aggregates stay cached.  Values that can change on race day (odds,
    popularity, entry details, body weight, condition and result) are refreshed
    from the read-only keiba database.  Historical result screens pass
    ``recompute_features=False``: their scores are never recalculated, which keeps
    an old purchase-history lookup fast while still taking order and payouts from
    the authoritative database.
    The returned object is a deep copy so request handlers can swap it atomically.
    """
    date = str(daily.get("date") or "")
    if len(date) != 8 or not daily.get("races"):
        return daily

    _ensure_keiba_on_path()
    from scripts.backtest import (  # type: ignore
        get_payout_row, horses_for_race, list_races, popularity_config,
        race_odds_untrusted,
    )

    refreshed = copy.deepcopy(daily)
    cached_by_id = {race["race_id"]: race for race in refreshed.get("races", [])}
    current_columns = [
        column for column in refreshed.get("columns", []) if column.get("kind") == "current"
    ]
    compute_columns = [
        column for column in refreshed.get("columns", []) if column.get("kind") == "compute"
    ]
    max_age = popularity_config().get("max_snapshot_age_min")
    refreshed_at = datetime.now().astimezone().isoformat(timespec="seconds")

    with open_conn() as conn:
        db_races = list_races(conn, date, date, jra_only=True, require_confirmed=False)
        for db_race in db_races:
            cached = cached_by_id.get(_race_id(db_race))
            if cached is None:
                continue
            live_horses = horses_for_race(conn, db_race)
            if not live_horses:
                continue
            live_race = _with_latest_weather(
                conn, _with_latest_race_changes(conn, db_race)
            )
            live_seg = mx._seg(live_race)
            next_parts = _feature_context_parts(live_seg, live_horses)
            next_feature_context = _feature_context(live_seg, live_horses)
            recompute_live_features = recompute_features and (
                cached.get("live_feature_context") != next_feature_context
            )
            if recompute_live_features:
                # **なぜ回ったのかを残す。** 1レースの再計算は十数秒・約400本の
                # DB問い合わせになるので、頻度が分からないと費用を見積もれない。
                logger.info(
                    "当日特徴量を再計算: race=%s 頭数=%d 理由=%s",
                    _race_id(db_race), len(live_horses),
                    " / ".join(_context_changes(cached.get("live_feature_parts"),
                                                next_parts)),
                )
                recompute_started = time.monotonic()
            inactive = _inactive_horses(conn, db_race)
            feature_cache: dict = {}
            live_by_num = {
                str(horse.get("horse_num") or "").zfill(2): horse for horse in live_horses
            }
            for horse in cached.get("horses", []):
                number = str(horse.get("num") or "").zfill(2)
                live = live_by_num.get(number)
                if live is None:
                    continue
                inactive_info = inactive.get(number)
                raw_odds = model._num(live.get("win_odds"))
                horse["order"] = live.get("confirmed_order")
                horse["odds"] = raw_odds / 10.0 if raw_odds and raw_odds > 0 else None
                horse["pop"] = model._num(live.get("win_popularity"))
                horse["waku"] = _waku(live)
                live_name = (live.get("horse_name") or "").strip()
                if live_name:
                    horse["name"] = live_name
                horse.update(_entry_info(live))
                horse["scratched"] = bool(inactive_info)
                horse["scratch_status"] = (
                    inactive_info.get("label") if inactive_info else None
                )
                features = horse.setdefault("x", {})
                for column in current_columns:
                    feature = model.FEATURES.get(column.get("key"))
                    if feature is not None:
                        features[column["id"]] = feature.metric(live)
                # 馬場状態・騎手・負担重量が変わった場合、表示だけでなく予想に使う
                # compute列も現在の入力で再計算する。日次キャッシュの古い採点を残さない。
                if recompute_live_features and not inactive_info and compute_columns:
                    computed = model._compute_features(
                        conn, live, live_race, feature_cache
                    )
                    for column in compute_columns:
                        feature = model.FEATURES.get(column.get("key"))
                        if feature is not None:
                            features[column["id"]] = feature.metric(computed)

            cached["start_time"] = _hhmm(live_race.get("start_time"))
            cached["seg"] = live_seg
            cached["race_title"] = _race_title(live_race)
            cached["race_class"] = _race_class(live_race)
            cached["race_name"] = _display_name(live_race, cached["seg"])
            cached.update(_change_metadata(live_race))
            cached["condition_as_of"] = live_race.get("condition_as_of")
            cached["live_feature_context"] = next_feature_context
            cached["live_feature_parts"] = next_parts
            if recompute_live_features:
                logger.info("当日特徴量を再計算: race=%s 完了 %.1f秒",
                            _race_id(db_race), time.monotonic() - recompute_started)
            cached["scratched_horses"] = [
                {"horse_num": number, **info} for number, info in sorted(inactive.items())
            ]
            cached["odds_as_of"] = _odds_as_of(live_horses)
            cached["weight_announced"] = _weight_announced(live_horses)
            cached["trusted"] = not race_odds_untrusted(live_horses, live_race, max_age)
            cached["tan"] = mx._tan_payouts(get_payout_row(conn, db_race))
            if any(h.get("order") == 1 for h in cached.get("horses", [])):
                cached["payouts"] = payout_data.for_race(cached)
            cached["live_updated_at"] = refreshed_at
            cached["live_revision"] = _live_revision(cached)

    refreshed["live_updated_at"] = refreshed_at
    return refreshed


def find_race(daily: dict, race_id: str) -> dict | None:
    return next((r for r in daily.get("races", []) if r["race_id"] == race_id), None)


def race_started(race: dict, *, now: datetime | None = None) -> bool:
    """サーバ時刻を基準に、予定発走時刻へ到達したかを返す。"""
    date = str(race.get("date") or "")
    start_time = str(race.get("start_time") or "")
    try:
        starts = datetime.strptime(date + start_time, "%Y%m%d%H:%M")
    except ValueError:
        return False
    current = now or datetime.now().astimezone()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    return current >= starts


def today_status(daily: dict, col_ids: list[str] | None = None,
                 *, now: datetime | None = None) -> list[dict]:
    """設計書 §8 `GET /api/races/today` 用のレース一覧 + 分析可否ステータス。

    馬体重待ち・ゲート未通過列を各レースについて返す (§5.3 の当日事前検査を兼ねる)。
    """
    from . import labels as lb
    from . import presets as ps
    cols = col_ids if col_ids is not None else [c["id"] for c in daily.get("columns", [])]
    prep = mx.prepare_races(daily) if daily.get("races") else []
    missing_by_index = {g["index"]: g["missing_columns"] for g in ps.gate_check(prep, cols)}

    out = []
    n_cols = len(cols)
    for i, r in enumerate(daily.get("races", [])):
        missing = missing_by_index.get(i, [])
        n_passed = n_cols - len(missing)
        seg = r.get("seg") or {}
        started = race_started({**r, "date": r.get("date") or daily.get("date")}, now=now)
        out.append({
            "race_id": r["race_id"],
            "date": r.get("date") or daily.get("date"),
            "race_num": r.get("race_num"),
            "race_name": r.get("race_name"),
            "race_title": r.get("race_title"),
            "race_class": r.get("race_class"),
            # UI の一覧表示用 (発走時刻・頭数・馬場条件) と「結果待ち」判定。
            # 日本語ラベルはサーバで付ける (labels.py が唯一の語彙表。UI 側に
            # 同じ対応表を複製すると片方だけ変わって静かにずれる)。
            "start_time": r.get("start_time"),
            "start_time_changed": bool(r.get("start_time_changed")),
            "original_start_time": r.get("original_start_time"),
            "start_time_change_as_of": r.get("start_time_change_as_of"),
            "course_changed": bool(r.get("course_changed")),
            "course_change": r.get("course_change"),
            # 1日に複数開催があるのでレース番号だけでは一意にならない
            # (実データ: 函館01R / 福島01R / 小倉01R が並ぶ)。UI は競馬場でまとめる。
            "track": seg.get("track"),
            "track_label": lb.value_label("track", seg.get("track")),
            "surface": seg.get("surface"),
            "surface_label": lb.value_label("surface", seg.get("surface")),
            "distance": seg.get("distance"),
            "condition": seg.get("condition"),
            "condition_label": ("馬場発表待ち" if seg.get("condition") == "unknown"
                                else lb.value_label("condition", seg.get("condition"))),
            "condition_announced": seg.get("condition") != "unknown",
            "condition_as_of": r.get("condition_as_of"),
            "finished": any(h.get("order") == 1 for h in r["horses"]),
            # 着順未取込でも、発走時刻を過ぎたレースを「予想できます」と表示しない。
            "started": started,
            "n_horses": sum(1 for h in r["horses"] if not h.get("scratched")),
            "n_scratched": sum(1 for h in r["horses"] if h.get("scratched")),
            "scratched_horses": r.get("scratched_horses") or [
                {"horse_num": h.get("num"), "horse_name": h.get("name"),
                 "label": h.get("scratch_status") or "取消・除外"}
                for h in r["horses"] if h.get("scratched")
            ],
            "weight_announced": r.get("weight_announced", False),
            "odds_trusted": r.get("trusted", False),
            "odds_as_of": r.get("odds_as_of"),
            "live_revision": r.get("live_revision") or _live_revision(r),
            "live_updated_at": r.get("live_updated_at") or daily.get("live_updated_at"),
            "n_columns": n_cols,
            "n_gate_passed": n_passed,
            "gate_pass_rate": round(n_passed / n_cols, 3) if n_cols else None,
            "gate_missing_columns": missing,
            "n_gate_missing": len(missing),
            # **予想が可能か** を表す。全 425 列の通過は要求しない
            # (実測: 直近レースでも中央値5列は欠ける。5列欠けただけで使えないのは誤り)。
            # 参加者が選んだ列が使えるかは /api/predict が per-config で警告する。
            "ready": any(not h.get("scratched") for h in r["horses"]) and n_passed > 0,
        })
    return out
