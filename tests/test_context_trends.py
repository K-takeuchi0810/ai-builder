from __future__ import annotations

from builder import context_trends


def _race(index: int, *, date: str = "20260701", track: str = "tokyo",
          distance: int = 1600, signal: bool = True) -> dict:
    horses = []
    for num in range(1, 11):
        # 人気薄の8～10番を項目上位かつ3着以内にし、人気だけでは説明できない
        # 合成傾向を作る。
        order = num - 7 if num >= 8 else num + 3
        value = float(num if signal else 11 - num)
        horses.append({
            "num": str(num), "order": order, "pop": num,
            "x": {"jockey_win_rate": value},
        })
    return {
        "race_id": f"H{index}", "date": date,
        "seg": {"track": track, "surface": "turf", "distance": distance,
                "distance_bucket": "mile"},
        "horses": horses,
    }


def _source(races: list[dict]) -> dict:
    return {
        "races": races,
        "columns": [{"id": "jockey_win_rate", "key": "jockey_win_rate",
                     "kind": "step1", "hib": True, "label": "騎手勝率"}],
    }


def _target(date: str = "20260801") -> dict:
    return {
        "race_id": "TARGET", "date": date,
        "seg": {"track": "tokyo", "surface": "turf", "distance": 1600,
                "distance_bucket": "mile"},
        "horses": [],
    }


def test_exact_course_distance_trend_builds_race_specific_config():
    result = context_trends.analyze(_source([_race(i) for i in range(60)]), _target())
    assert result["available"] is True
    assert result["scope"]["key"] == "exact"
    assert result["scope"]["n_races"] == 60
    assert result["items"][0]["key"] == "jockey_win_rate"
    assert result["items"][0]["popularity_adjusted_lift"] > 0
    assert result["recommended_config"]["step1"] == ["jockey_win_rate"]


def test_scope_falls_back_to_same_track_and_distance_band():
    races = [_race(i, distance=1400 if i < 30 else 1800) for i in range(60)]
    result = context_trends.analyze(_source(races), _target())
    assert result["available"] is True
    assert result["scope"]["key"] == "track_band"
    assert result["scope"]["n_races"] == 60


def test_future_results_are_never_used_for_recommendation():
    future = [_race(i, date="20260802") for i in range(80)]
    result = context_trends.analyze(_source(future), _target("20260801"))
    assert result["available"] is False
    assert result["scope"]["n_races"] == 0


def test_previous_calendar_day_is_returned_as_a_separate_reference_trend():
    history = [_race(i, date="20260701") for i in range(60)]
    previous_day = [_race(100 + i, date="20260731") for i in range(4)]
    result = context_trends.analyze(_source(history + previous_day), _target("20260801"))
    assert result["available"] is True
    assert result["recent"]["available"] is True
    assert result["recent"]["date"] == "20260731"
    assert result["recent"]["scope"] == "exact"
    assert result["recent"]["n_races"] == 4
    assert result["recent"]["items"][0]["reliable"] is False
    assert "前日＋過去傾向" in result["recommended_config"]["name"]


def test_previous_day_prefers_distance_band_before_all_distances():
    history = [_race(i, date="20260701") for i in range(60)]
    previous_day = [_race(100 + i, date="20260731",
                          distance=1400 if i < 2 else 1800) for i in range(4)]
    result = context_trends.analyze(_source(history + previous_day), _target("20260801"))
    assert result["recent"]["available"] is True
    assert result["recent"]["scope"] == "distance_band"
    assert result["recent"]["n_races"] == 4
    assert "同じ距離帯" in result["recent"]["label"]
