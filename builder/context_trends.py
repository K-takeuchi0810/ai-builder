"""競馬場・芝ダート・距離条件ごとの、過去データだけを使った項目傾向。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import labels as lb
from . import specs as sp


MIN_SCOPE_RACES = 50
MIN_FEATURE_RACES = 30
TOP_PER_RACE = 3
MAX_ITEMS = 5


def _num(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _pop_bucket(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if n == 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 6:
        return "4-6"
    return "7+"


def _scope_candidates(races: list[dict], target: dict) -> list[tuple[str, str, list[dict]]]:
    seg = target.get("seg") or {}
    track, surface = str(seg.get("track") or ""), str(seg.get("surface") or "")
    distance, band = seg.get("distance"), str(seg.get("distance_bucket") or "")
    past = [r for r in races if str(r.get("date") or "") < str(target.get("date") or "")
            and any(isinstance(h.get("order"), int) and h["order"] == 1
                    for h in r.get("horses", []))]

    exact = [r for r in past if str((r.get("seg") or {}).get("track") or "") == track
             and str((r.get("seg") or {}).get("surface") or "") == surface
             and (r.get("seg") or {}).get("distance") == distance]
    track_band = [r for r in past
                  if str((r.get("seg") or {}).get("track") or "") == track
                  and str((r.get("seg") or {}).get("surface") or "") == surface
                  and str((r.get("seg") or {}).get("distance_bucket") or "") == band]
    broad = [r for r in past if str((r.get("seg") or {}).get("surface") or "") == surface
             and str((r.get("seg") or {}).get("distance_bucket") or "") == band]
    track_label = lb.value_label("track", track) or track
    surface_label = lb.value_label("surface", surface) or surface
    return [
        ("exact", f"{track_label}・{surface_label}{distance}m", exact),
        ("track_band", f"{track_label}・{surface_label}・同じ距離帯", track_band),
        ("surface_band", f"全競馬場・{surface_label}・同じ距離帯", broad),
    ]


def _selection(column: dict) -> dict | None:
    key = str(column.get("key") or "")
    if key in sp.PARTICIPANT_EXCLUDED_KEYS:
        return None
    if key in sp.MAIB_STEP1_KEYS:
        return {"kind": "step1", "key": key}
    match = list(column.get("match") or [])
    lookback = column.get("lookback")
    if (key in sp.MAIB_STEP2_METRICS and match in sp.MAIB_MATCHES
            and lookback in sp.MAIB_LOOKBACKS):
        return {"kind": "step2", "metric": key, "match": match,
                "lookback": lookback}
    return None


def _pop_rates(races: list[dict]) -> tuple[dict[str, float], float]:
    counts: dict[str, list[int]] = {}
    total = hits = 0
    for race in races:
        for horse in race.get("horses", []):
            order = horse.get("order")
            if not isinstance(order, int) or order <= 0:
                continue
            bucket = _pop_bucket(horse.get("pop"))
            cell = counts.setdefault(bucket, [0, 0])
            cell[0] += 1
            cell[1] += int(order <= 3)
            total += 1
            hits += int(order <= 3)
    overall = hits / total if total else 0.0
    return ({key: got / n for key, (n, got) in counts.items() if n}
            | {"unknown": overall}), overall


def _measure(column: dict, races: list[dict], pop_rates: dict[str, float],
             overall: float, *, min_races: int = MIN_FEATURE_RACES,
             shrink_target: int = 300) -> dict | None:
    selection = _selection(column)
    if not selection:
        return None
    cid = str(column.get("id") or "")
    high_is_better = bool(column.get("hib"))
    n_races = candidates = hits = 0
    expected_hits = baseline_hits = 0.0
    for race in races:
        values = []
        all_valid = []
        for horse in race.get("horses", []):
            order = horse.get("order")
            if not isinstance(order, int) or order <= 0:
                continue
            all_valid.append(horse)
            value = _num((horse.get("x") or {}).get(cid))
            if value is not None:
                values.append((value, horse))
        if len(values) < 5 or len({v for v, _h in values}) < 2:
            continue
        values.sort(key=lambda pair: pair[0], reverse=high_is_better)
        chosen = [horse for _value, horse in values[:min(TOP_PER_RACE, len(values))]]
        n_races += 1
        candidates += len(chosen)
        hits += sum(1 for horse in chosen if int(horse["order"]) <= 3)
        expected_hits += sum(pop_rates.get(_pop_bucket(horse.get("pop")), overall)
                             for horse in chosen)
        baseline_hits += sum(1 for horse in all_valid if int(horse["order"]) <= 3) \
            * len(chosen) / len(all_valid)
    if n_races < min_races or candidates <= 0:
        return None
    place_rate = hits / candidates
    expected_rate = expected_hits / candidates
    baseline_rate = baseline_hits / candidates
    adjusted = place_rate - expected_rate
    raw = place_rate - baseline_rate
    # 小標本を上位にしすぎないため、300レースまでは滑らかに縮小する。
    score = adjusted * min(1.0, math.sqrt(n_races / float(shrink_target)))
    standard_error = math.sqrt(max(place_rate * (1 - place_rate), .0001) / candidates)
    return {
        "id": cid,
        "key": str(column.get("key") or ""),
        "label": lb.column_label(str(column.get("key") or ""),
                                  column.get("match"), column.get("lookback")),
        "n_races": n_races,
        "n_candidates": candidates,
        "place_rate": round(place_rate, 4),
        "baseline_rate": round(baseline_rate, 4),
        "popularity_adjusted_lift": round(adjusted, 4),
        "raw_lift": round(raw, 4),
        "reliable": adjusted > 1.64 * standard_error,
        "score": score,
        "selection": selection,
    }


def _distinct(measured: list[dict], limit: int = MAX_ITEMS) -> list[dict]:
    """同じ指標の期間違いだけで一覧を埋めず、種類を優先する。"""
    items, used_keys = [], set()
    for item in measured:
        if item["key"] in used_keys:
            continue
        items.append(item)
        used_keys.add(item["key"])
        if len(items) >= limit:
            break
    return items


def _config(items: list[dict], name: str) -> dict:
    step1 = [item["selection"]["key"] for item in items
             if item["selection"]["kind"] == "step1"]
    step2 = [{key: sel[key] for key in ("metric", "match", "lookback")}
             for item in items for sel in [item["selection"]]
             if sel["kind"] == "step2"]
    return {"name": name, "step1": step1, "step2": step2}


def _previous_date(value: str) -> str | None:
    try:
        return (datetime.strptime(str(value), "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return None


def analyze(source: dict, target: dict) -> dict:
    """対象レース以前だけで条件を選び、項目上位馬の複勝傾向を返す。"""
    candidates = _scope_candidates(source.get("races") or [], target)
    selected = next((row for row in candidates if len(row[2]) >= MIN_SCOPE_RACES), None)
    if selected is None:
        selected = candidates[-1] if candidates else ("none", "データなし", [])
    scope_key, scope_label, races = selected
    if len(races) < MIN_SCOPE_RACES:
        return {"available": False, "scope": {"key": scope_key, "label": scope_label,
                "n_races": len(races)}, "items": [],
                "message": "この条件は過去レース数が少なく、傾向を表示できません"}

    pop_rates, overall = _pop_rates(races)
    columns = source.get("columns") or []
    measured = [_measure(column, races, pop_rates, overall) for column in columns]
    measured = [item for item in measured if item and item["score"] > 0]
    measured.sort(key=lambda item: (item["score"], item["n_races"]), reverse=True)
    items = _distinct(measured)

    # 前日だけの値を中長期の人気帯基準と比較する。前日数レースだけで
    # 「傾向明瞭」と断定せず、直近の馬場バイアスを見る参考情報として返す。
    seg = target.get("seg") or {}
    previous = _previous_date(str(target.get("date") or ""))
    recent_base = [race for race in source.get("races") or []
                   if str(race.get("date") or "") == previous
                   and str((race.get("seg") or {}).get("track") or "")
                   == str(seg.get("track") or "")
                   and str((race.get("seg") or {}).get("surface") or "")
                   == str(seg.get("surface") or "")
                   and any(isinstance(h.get("order"), int) and h["order"] == 1
                           for h in race.get("horses", []))]
    recent_exact = [race for race in recent_base
                    if (race.get("seg") or {}).get("distance") == seg.get("distance")]
    recent_band = [race for race in recent_base
                   if str((race.get("seg") or {}).get("distance_bucket") or "")
                   == str(seg.get("distance_bucket") or "")]
    recent_candidates = [
        ("exact", recent_exact), ("distance_band", recent_band), ("all_distance", recent_base),
    ]
    recent_scope, recent_races = next(
        ((key, rows) for key, rows in recent_candidates if len(rows) >= 2),
        ("all_distance", recent_base),
    )
    recent_measured = [_measure(column, recent_races, pop_rates, overall,
                                min_races=2, shrink_target=12)
                       for column in columns]
    recent_measured = [item for item in recent_measured if item and item["score"] > 0]
    recent_measured.sort(key=lambda item: (item["score"], item["n_races"]), reverse=True)
    recent_items = _distinct(recent_measured)
    for item in recent_items:
        item["reliable"] = False

    # 適用設定は前日だけに全賭けしない。前日と中長期の両方でプラスの項目を
    # 優先し、足りない分を安定した中長期項目で補う。
    stable_keys = {item["key"] for item in measured}
    recommended = [item for item in recent_items if item["key"] in stable_keys]
    used = {item["key"] for item in recommended}
    recommended.extend(item for item in items if item["key"] not in used)
    recommended = recommended[:MAX_ITEMS]
    track_label = lb.value_label("track", seg.get("track")) or str(seg.get("track") or "")
    surface_label = lb.value_label("surface", seg.get("surface")) or str(seg.get("surface") or "")
    distance_label = f"{seg.get('distance')}m"
    recent_scope_label = {
        "exact": f"{track_label}・{surface_label}{distance_label}",
        "distance_band": f"{track_label}・{surface_label}・同じ距離帯",
        "all_distance": f"{track_label}・{surface_label}・全距離",
    }[recent_scope]
    recent = {
        "available": bool(recent_items), "date": previous,
        "scope": recent_scope, "label": recent_scope_label,
        "n_races": len(recent_races),
        "items": recent_items,
        "message": ("前日の同競馬場・同馬場の結果です。レース数が少ないため参考傾向です"
                    if recent_items else "前日の同競馬場・同馬場では比較できる結果が不足しています"),
    }
    return {
        "available": bool(items),
        "race_id": target.get("race_id"),
        "scope": {"key": scope_key, "label": scope_label, "n_races": len(races)},
        "items": items,
        "recent": recent,
        "recommended_config": _config(
            recommended, f"{track_label} 前日＋過去傾向" if recent_items else f"{scope_label} 推奨"),
        "message": ("対象レースより前の結果だけを使用。人気帯の差を補正した参考傾向です"
                    if items else "安定して比較できる項目がありません"),
    }
