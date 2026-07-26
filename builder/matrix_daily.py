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

import json
from pathlib import Path

from . import config, matrix as mx, model
from .keiba_bridge import _ensure_keiba_on_path, open_conn

# v2: UI が必要とする start_time / 確定状態を各レースに持たせた。
# v3: 枠番 (waku) を各馬に持たせた。UI が馬番から計算していたため誤った枠色が
#     出ていた (7頭立てで 6/7 件外れる)。枠割は頭数依存なので UI 導出は不可能。
# v4: レースのクラス/名称 (race_class / race_title) と、各馬の出走情報
#     (騎手・斤量・調教師・性齢・馬体重と増減) を持たせた。
#     「騎手の成績」を根拠に印を打ちながら騎手名を出していなかったため。
DAILY_VERSION = 4


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
                        x[c["id"]] = model._aggregate_value(feat, past, race,
                                                            c["match"], c["lookback"])
                    else:
                        x[c["id"]] = feat.metric(h)
                wo = model._num(h.get("win_odds"))
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
                    "x": x,
                })
            seg = mx._seg(race)
            races_out.append({
                "race_id": _race_id(race),
                "date": before,
                "race_num": str(race.get("race_num")),
                # 名称とクラスを **別のスロット** に持つ。表示名スロットに条件を
                # 焼き込むと、実名を持つ特別戦で名前が条件を上書きして芝/ダート・
                # 距離が画面から消える。UI は 1行目=名称かクラス、2行目=条件 に分ける。
                "race_title": _race_title(race),
                "race_class": _race_class(race),
                "race_name": _display_name(race, seg),   # 後方互換 (旧UI用)
                "start_time": _hhmm(race.get("start_time")),   # UI の発走時刻表示用
                "seg": seg,
                "horses": hrows,
                "tan": mx._tan_payouts(get_payout_row(conn, race)),
                "trusted": not race_odds_untrusted(horses, race, max_age),
                "odds_as_of": _odds_as_of(horses),           # UI はオッズに取得時刻を添える
                "weight_announced": _weight_announced(horses),
            })

    daily = {"version": DAILY_VERSION, "date": date, "from": date, "to": date,
             "columns": cols, "races": races_out}
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
        # 斤量は 0.1kg 単位で格納されている (555 → 55.5kg)
        "burden_weight": (bw / 10.0) if bw else None,
        "trainer": (h.get("trainer_short_name") or "").strip() or None,
        "sex_age": (f"{sex}{int(age)}" if sex and age else None),
        "horse_weight": int(hw) if hw.isdigit() else None,
        "horse_weight_change": change,
    }


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


def load_daily(date: str, specs: list[dict]) -> dict:
    p = _daily_path(date, mx._columns(specs))
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def find_race(daily: dict, race_id: str) -> dict | None:
    return next((r for r in daily.get("races", []) if r["race_id"] == race_id), None)


def today_status(daily: dict, col_ids: list[str] | None = None) -> list[dict]:
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
        out.append({
            "race_id": r["race_id"],
            "race_num": r.get("race_num"),
            "race_name": r.get("race_name"),
            "race_title": r.get("race_title"),
            "race_class": r.get("race_class"),
            # UI の一覧表示用 (発走時刻・頭数・馬場条件) と「結果待ち」判定。
            # 日本語ラベルはサーバで付ける (labels.py が唯一の語彙表。UI 側に
            # 同じ対応表を複製すると片方だけ変わって静かにずれる)。
            "start_time": r.get("start_time"),
            # 1日に複数開催があるのでレース番号だけでは一意にならない
            # (実データ: 函館01R / 福島01R / 小倉01R が並ぶ)。UI は競馬場でまとめる。
            "track": seg.get("track"),
            "track_label": lb.value_label("track", seg.get("track")),
            "surface": seg.get("surface"),
            "surface_label": lb.value_label("surface", seg.get("surface")),
            "distance": seg.get("distance"),
            "condition": seg.get("condition"),
            "condition_label": lb.value_label("condition", seg.get("condition")),
            "finished": any(h.get("order") == 1 for h in r["horses"]),
            "n_horses": len(r["horses"]),
            "weight_announced": r.get("weight_announced", False),
            "odds_trusted": r.get("trusted", False),
            "odds_as_of": r.get("odds_as_of"),
            "n_columns": n_cols,
            "n_gate_passed": n_passed,
            "gate_pass_rate": round(n_passed / n_cols, 3) if n_cols else None,
            "gate_missing_columns": missing,
            "n_gate_missing": len(missing),
            # **予想が可能か** を表す。全 425 列の通過は要求しない
            # (実測: 直近レースでも中央値5列は欠ける。5列欠けただけで使えないのは誤り)。
            # 参加者が選んだ列が使えるかは /api/predict が per-config で警告する。
            "ready": bool(r["horses"]) and n_passed > 0,
        })
    return out
