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

DAILY_VERSION = 1


def _daily_dir() -> Path:
    return Path(config.CORNER_INDEX_PATH).parent / "daily"


def _daily_path(date: str, cols: list[dict]) -> Path:
    return _daily_dir() / f"daily_{date}_{mx._col_hash(cols)}.json"


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
                    "order": h.get("confirmed_order"),          # 当日は 0/None
                    "odds": (wo / 10.0) if (wo and wo > 0) else None,
                    "pop": model._num(h.get("win_popularity")),
                    "name": (h.get("horse_name") or "").strip(),
                    "n_past_runs": len(past),                   # カバレッジ表示用
                    "x": x,
                })
            races_out.append({
                "race_id": _race_id(race),
                "date": before,
                "race_num": str(race.get("race_num")),
                "race_name": (race.get("race_name") or "").strip(),
                "seg": mx._seg(race),
                "horses": hrows,
                "tan": mx._tan_payouts(get_payout_row(conn, race)),
                "trusted": not race_odds_untrusted(horses, race, max_age),
                "weight_announced": _weight_announced(horses),
            })

    daily = {"version": DAILY_VERSION, "date": date, "from": date, "to": date,
             "columns": cols, "races": races_out}
    _daily_dir().mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(daily, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return daily


def _race_id(race: dict) -> str:
    return (f"{race.get('race_year')}{race.get('race_month_day')}"
            f"{race.get('track_code')}{race.get('kaiji')}"
            f"{race.get('nichiji')}{race.get('race_num')}")


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
    from . import presets as ps
    cols = col_ids if col_ids is not None else [c["id"] for c in daily.get("columns", [])]
    prep = mx.prepare_races(daily) if daily.get("races") else []
    missing_by_index = {g["index"]: g["missing_columns"] for g in ps.gate_check(prep, cols)}

    out = []
    for i, r in enumerate(daily.get("races", [])):
        missing = missing_by_index.get(i, [])
        out.append({
            "race_id": r["race_id"],
            "race_num": r.get("race_num"),
            "race_name": r.get("race_name"),
            "n_horses": len(r["horses"]),
            "weight_announced": r.get("weight_announced", False),
            "odds_trusted": r.get("trusted", False),
            "gate_missing_columns": missing,
            "n_gate_missing": len(missing),
            "ready": bool(r["horses"]) and not missing,
        })
    return out
