"""選択買い目のオッズ表示。未発表値を推測しないことを固定する。"""

from __future__ import annotations

import sqlite3

from builder import odds


def _race(*, finished=False):
    return {
        "date": "20260801", "race_num": "01",
        "seg": {"track": "04", "kaiji": "02", "nichiji": "03"},
        "odds_as_of": "2026-08-01T09:20:00",
        "horses": [
            {"num": "01", "odds": 4.2, "pop": 2, "order": 1 if finished else 0},
            {"num": "02", "odds": None, "pop": 0, "order": 0},
        ],
    }


def _db(path, *, data_div="1"):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE exotic_odds (
      race_year TEXT, race_month_day TEXT, track_code TEXT, kaiji TEXT,
      nichiji TEXT, race_num TEXT, bet_type TEXT, combo TEXT,
      odds_low INTEGER, odds_high INTEGER, popularity INTEGER,
      data_div TEXT, announced_time TEXT)""")
    conn.execute("INSERT INTO exotic_odds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("2026", "0801", "04", "02", "03", "01", "quinella", "0102",
                  125, 0, 3, data_div, "09200000"))
    conn.commit()
    conn.close()


def test_tan_uses_the_current_horse_odds(tmp_path):
    db = tmp_path / "empty.sqlite"
    _db(db)
    slip = [{"key": "tan", "combos": [["01"], ["02"]]}]
    odds.attach(_race(), slip, db_path=db)
    assert slip[0]["odds"][0] == {
        "available": True, "status": "provisional", "low": 4.2, "high": None,
        "popularity": 2, "as_of": "2026-08-01T09:20:00",
    }
    assert slip[0]["odds"][1]["available"] is False


def test_race_id_supplies_missing_meeting_keys_from_daily_cache():
    race = _race()
    race["race_id"] = "2026080104020301"
    race["seg"].pop("kaiji")
    race["seg"].pop("nichiji")
    assert odds._race_keys(race) == ("2026", "0801", "04", "02", "03", "01")


def test_tan_refreshes_from_the_read_only_database_without_rebuilding_cache(tmp_path):
    db = tmp_path / "live.sqlite"
    _db(db)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE horse_races (
      race_year TEXT, race_month_day TEXT, track_code TEXT, kaiji TEXT,
      nichiji TEXT, race_num TEXT, horse_num TEXT, win_odds INTEGER,
      win_popularity INTEGER, odds_fetched_at TEXT)""")
    conn.execute("INSERT INTO horse_races VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("2026", "0801", "04", "02", "03", "01", "01", 67, 4,
                  "2026-08-01T09:30:00"))
    conn.commit(); conn.close()
    slip = [{"key": "tan", "combos": [["01"]]}]
    odds.attach(_race(), slip, db_path=db)
    assert slip[0]["odds"][0]["low"] == 6.7
    assert slip[0]["odds"][0]["as_of"] == "2026-08-01T09:30:00"


def test_pre_race_exotic_odds_match_the_selected_combo(tmp_path):
    db = tmp_path / "odds.sqlite"
    _db(db, data_div="1")
    slip = [{"key": "umaren", "combos": [["02", "01"]]}]
    odds.attach(_race(), slip, db_path=db)
    got = slip[0]["odds"][0]
    assert got["available"] is True and got["low"] == 12.5
    assert got["popularity"] == 3 and got["status"] == "provisional"


def test_final_odds_never_leak_into_an_upcoming_race(tmp_path):
    db = tmp_path / "final.sqlite"
    _db(db, data_div="5")
    slip = [{"key": "umaren", "combos": [["01", "02"]]}]
    odds.attach(_race(finished=False), slip, db_path=db)
    assert slip[0]["odds"][0]["available"] is False


def test_final_odds_are_allowed_after_the_result(tmp_path):
    db = tmp_path / "final.sqlite"
    _db(db, data_div="5")
    slip = [{"key": "umaren", "combos": [["01", "02"]]}]
    odds.attach(_race(finished=True), slip, db_path=db)
    assert slip[0]["odds"][0]["status"] == "final"


def _o1_record(*, data_div="1"):
    rec = bytearray(b" " * 963)
    rec[:2] = b"O1"
    rec[2:3] = data_div.encode("ascii")
    rec[11:27] = b"2026080104020301"
    rec[27:35] = b"08010920"
    rec[267:279] = b"010211032716"
    rec[279:291] = b"020059009010"
    rec[603:612] = b"110587135"
    rec[612:621] = b"120062821"
    return bytes(rec)


def test_place_and_bracket_odds_are_read_from_jvdata_o1(tmp_path, monkeypatch):
    raw = tmp_path / "data" / "raw" / "0B31"
    raw.mkdir(parents=True)
    (raw / "0B31_2026080104020301_1.jvd").write_bytes(_o1_record())
    monkeypatch.setattr(odds.config, "KEIBA_YOSOU_PATH", tmp_path)
    db = tmp_path / "empty.sqlite"
    _db(db)
    slip = [
        {"key": "fuku", "combos": [["01"], ["02"]]},
        {"key": "wakuren", "combos": [["2", "1"]]},
    ]
    odds.attach(_race(), slip, db_path=db)
    assert slip[0]["odds"][0]["low"] == 21.1
    assert slip[0]["odds"][0]["high"] == 32.7
    assert slip[0]["odds"][1]["low"] == 5.9
    assert slip[1]["odds"][0]["low"] == 62.8


def test_post_race_o1_odds_do_not_leak_before_start(tmp_path, monkeypatch):
    raw = tmp_path / "data" / "raw" / "0B31"
    raw.mkdir(parents=True)
    (raw / "0B31_2026080104020301_1.jvd").write_bytes(_o1_record(data_div="4"))
    monkeypatch.setattr(odds.config, "KEIBA_YOSOU_PATH", tmp_path)
    db = tmp_path / "empty.sqlite"
    _db(db)
    slip = [{"key": "fuku", "combos": [["01"]]}]
    odds.attach(_race(), slip, db_path=db)
    assert slip[0]["odds"][0]["available"] is False
