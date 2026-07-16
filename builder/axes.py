"""セグメント軸の導出 (pure)。生のレース/馬の値から探索軸の辞書を作る。

keiba-yosou にも DB にも依存しない純粋関数なのでテスト可能。bucket 境界は
keiba-yosou の distance_bucket_label 等に後で寄せてもよい (README 参照)。
"""

from __future__ import annotations

# JRA 馬場状態コード → ラベル (turf_condition / dirt_condition 共通: 1良2稍重3重4不良)
_CONDITION = {"1": "firm", "2": "good", "3": "yielding", "4": "soft"}
# JRA 天候コード: 1晴 2曇 3小雨 4雨 5小雪 6雪
_WEATHER = {"1": "clear", "2": "cloudy", "3": "light_rain",
            "4": "rain", "5": "light_snow", "6": "snow"}
_WET = {"1": "dry", "2": "dry", "3": "wet", "4": "wet", "5": "wet", "6": "wet"}


def distance_bucket(distance: int | None) -> str:
    if not distance:
        return "unknown"
    if distance <= 1300:
        return "sprint"
    if distance <= 1899:
        return "mile"
    if distance <= 2100:
        return "middle"
    return "long"


def season_of(month: str) -> str:
    m = month.lstrip("0") or "0"
    try:
        mi = int(m)
    except ValueError:
        return "unknown"
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "autumn", 10: "autumn", 11: "autumn"}.get(mi, "unknown")


def meet_progress(nichiji: str) -> str:
    try:
        n = int(nichiji)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 2:
        return "early"
    if n <= 5:
        return "mid"
    return "late"


def popularity_band(pop: int | None) -> str:
    if not pop:
        return "unknown"
    if pop <= 3:
        return str(pop)
    if pop <= 6:
        return "4-6"
    if pop <= 9:
        return "7-9"
    return "10+"


def condition_key(surface: str, turf_condition: str | None, dirt_condition: str | None) -> str:
    raw = turf_condition if surface == "turf" else dirt_condition
    if surface == "jump":  # 障害は populated な方
        raw = turf_condition or dirt_condition
    return _CONDITION.get((raw or "").strip(), "unknown")


def derive_axes(
    *,
    track: str,
    surface: str,
    distance: int | None,
    turf_condition: str | None,
    dirt_condition: str | None,
    weather_code: str | None,
    race_month_day: str,      # "MMDD"
    kaiji: str | None,
    nichiji: str | None,
    popularity: int | None,
    sire_line: str,
    sire_country: str,
    dam_sire_line: str,
    dam_sire_country: str,
) -> dict[str, str]:
    """探索軸の辞書を返す。値は全て文字列 (数値軸も文字列化して gte/lte で扱う)。"""
    month = (race_month_day or "0000")[:2]
    wcode = (weather_code or "").strip()
    return {
        "track": track,
        "surface": surface,
        "distance": distance_bucket(distance),
        "distance_m": str(distance or ""),
        "condition": condition_key(surface, turf_condition, dirt_condition),
        "weather": _WEATHER.get(wcode, "unknown"),
        "weather_wet": _WET.get(wcode, "unknown"),
        "meet_progress": meet_progress(nichiji),
        "kaiji": (kaiji or "").lstrip("0") or "unknown",
        "day_of_meet": (nichiji or "").lstrip("0") or "unknown",
        "month": month.lstrip("0") or "unknown",
        "season": season_of(month),
        "popularity": popularity_band(popularity),
        "sire_line": sire_line,
        "sire_country": sire_country,
        "dam_sire_line": dam_sire_line,
        "dam_sire_country": dam_sire_country,
    }


# 探索 UI / API で選択可能な軸の一覧 (表示順)
AXES = [
    "track", "surface", "distance", "condition", "weather", "weather_wet",
    "meet_progress", "kaiji", "month", "season", "popularity",
    "sire_line", "sire_country", "dam_sire_line", "dam_sire_country",
]
