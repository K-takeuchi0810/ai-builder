from __future__ import annotations

from builder import payouts, purchases


def _hr_record():
    rec = bytearray(b" " * 719)
    rec[:2] = b"HR"
    rec[11:27] = b"2026080107020301"
    # 単勝: 組番2 + 払戻9 + 人気2
    rec[102:115] = b"07" + b"000000550" + b"01"
    # 馬連: 組番4 + 払戻9 + 人気3
    rec[245:261] = b"0607" + b"000001900" + b"004"
    return bytes(rec)


def test_all_ticket_payout_parser_and_settlement():
    parsed = payouts.parse_hr(_hr_record())
    assert parsed == [
        {"type": "tan", "label": "単勝", "combo": [7], "text": "7",
         "payout_yen_per_100": 550, "popularity": 1},
        {"type": "umaren", "label": "馬連", "combo": [6, 7], "text": "6-7",
         "payout_yen_per_100": 1900, "popularity": 4},
    ]
    settled = payouts.settle([
        {"type": "tan", "combo": [7], "amount_yen": 200},
        {"type": "umaren", "combo": [7, 6], "amount_yen": 100},
        {"type": "fuku", "combo": [3], "amount_yen": 100},
    ], parsed)
    assert settled["invested_yen"] == 400
    assert settled["returned_yen"] == 3000
    assert settled["hit_points"] == 2


def test_purchase_is_saved_then_confirmed_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    race = {"race_id": "2026080107020301", "date": "20260801", "race_num": "01",
            "race_class": "未勝利", "horses": []}
    items = [{"type": "tan", "label": "単勝", "combo": [7],
              "text": "7", "amount_yen": 200}]
    first = purchases.record("owner", race=race, config_id="ai1", items=items, now=1000)
    same = purchases.record("owner", race=race, config_id="ai1", items=items, now=1100)
    assert same["id"] == first["id"]
    assert purchases.confirm("owner", first["id"], now=1200)["status"] == "purchased"
    summary = purchases.list_for_date("owner", "20260801", [race])
    assert summary["confirmed_races"] == 1
    assert summary["purchased_yen"] == 200


def test_late_manual_purchase_is_personal_record_but_not_ranked(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    race = {"race_id": "R1", "date": "20260801", "race_num": "01",
            "start_time": "10:00", "horses": [{"num": "07", "order": 1}]}
    ticket = [{"type": "tan", "label": "単勝", "combo": [7],
               "text": "7", "amount_yen": 300}]
    row = purchases.record("alice", race=race, config_id=None, items=ticket,
                           status="purchased", source="manual_record",
                           now=purchases._start_at(race) + 60)
    personal = purchases.list_for_date("alice", "20260801", [race])
    assert row["registered_before_start"] is False
    assert personal["confirmed_races"] == 1 and personal["purchased_yen"] == 300
    assert purchases.user_leaderboard("20260801", [race], {})["entries"] == []


def test_purchase_delete_is_owner_scoped_and_hidden_from_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    race = {"race_id": "R1", "date": "20260801", "race_num": "01",
            "start_time": "10:00", "horses": []}
    ticket = [{"type": "tan", "label": "単勝", "combo": [7],
               "text": "7", "amount_yen": 100}]
    row = purchases.record("alice", race=race, config_id=None, items=ticket,
                           status="purchased", now=1000)
    assert purchases.delete_record("bob", row["id"], now=1100) is None
    assert purchases.delete_record("alice", row["id"], now=1100)["ok"] is True
    assert purchases.confirm("alice", row["id"], now=1200) is None
    assert purchases.list_for_date("alice", "20260801", [race])["entries"] == []
    assert purchases.recorded_dates("alice") == []


def test_user_leaderboard_uses_latest_pre_start_entry_only(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    monkeypatch.setattr(purchases.payouts, "for_race", lambda _race: [{
        "type": "tan", "label": "単勝", "combo": [7], "text": "7",
        "payout_yen_per_100": 500, "popularity": 2,
    }])
    monkeypatch.setattr(purchases.labels, "value_label", lambda axis, value: "新潟")
    race = {
        "race_id": "2026080107020301", "date": "20260801", "race_num": "01",
        "race_class": "未勝利", "start_time": "12:00",
        "seg": {"track": "07"},
        "horses": [{"num": "07", "order": 1}],
    }
    start = purchases._start_at(race)
    assert start is not None

    def item(amount):
        return [{"type": "tan", "label": "単勝", "combo": [7],
                 "text": "7", "amount_yen": amount}]

    cfg = {"name": "東京マイル用", "step1": ["jockey_win_rate"], "step2": []}
    purchases.record("alice", race=race, config_id="ai1", items=item(100),
                     config_name=cfg["name"], config_snapshot=cfg, now=start - 300)
    purchases.record("alice", race=race, config_id="ai1", items=item(200),
                     config_name=cfg["name"], config_snapshot=cfg, now=start - 100)
    # 発走後に好結果を見て作った登録は成績に混ぜない。
    purchases.record("alice", race=race, config_id="ai1", items=item(1000),
                     config_name=cfg["name"], config_snapshot=cfg, now=start + 10)

    result = purchases.user_leaderboard("20260801", [race], {"alice": "利用者A"})
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["display_name"] == "利用者A"
    assert entry["registered_races"] == 1
    assert entry["registered_yen"] == 200
    assert entry["invested_yen"] == 200
    assert entry["returned_yen"] == 1000
    assert entry["roi"] == 5.0
    assert entry["provisional"] is True and entry["rank"] is None
    assert entry["details"][0]["config_name"] == "東京マイル用"
    assert entry["details"][0]["date"] == "20260801"
    assert entry["details"][0]["track_label"] == "新潟"
    assert entry["details"][0]["start_time"] == "12:00"
    assert "騎手" in entry["details"][0]["factors"][0]


def test_purchase_summary_marks_unconfirmed_amount_and_race_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    monkeypatch.setattr(purchases.labels, "value_label", lambda axis, value: "中京")
    race = {"race_id": "2026080107020301", "date": "20260801", "race_num": "09",
            "race_class": "1勝クラス", "start_time": "14:35",
            "seg": {"track": "07"}, "horses": []}
    purchases.record("owner", race=race, config_id=None,
                     items=[{"type": "tan", "label": "単勝", "combo": [7],
                             "text": "7", "amount_yen": 200}], now=1000)
    summary = purchases.list_for_date("owner", "20260801", [race])
    assert summary["unconfirmed_races"] == 1
    assert summary["unconfirmed_yen"] == 200
    assert summary["entries"][0]["track_label"] == "中京"
    assert summary["entries"][0]["start_time"] == "14:35"
    assert summary["updated_at"]


def test_unsettled_registration_does_not_lower_roi(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    monkeypatch.setattr(purchases.payouts, "for_race", lambda _race: [{
        "type": "tan", "label": "単勝", "combo": [7], "text": "7",
        "payout_yen_per_100": 300, "popularity": 1,
    }])
    finished = {"race_id": "R1", "date": "20260801", "race_num": "01",
                "start_time": "10:00", "horses": [{"num": "07", "order": 1}]}
    waiting = {"race_id": "R2", "date": "20260801", "race_num": "02",
               "start_time": "16:00", "horses": [{"num": "07", "order": 0}]}
    ticket = [{"type": "tan", "label": "単勝", "combo": [7],
               "text": "7", "amount_yen": 100}]
    purchases.record("alice", race=finished, config_id=None, items=ticket,
                     now=purchases._start_at(finished) - 60)
    purchases.record("alice", race=waiting, config_id=None,
                     items=[{**ticket[0], "amount_yen": 1000}],
                     now=purchases._start_at(waiting) - 60)
    entry = purchases.user_leaderboard("20260801", [finished, waiting], {})["entries"][0]
    assert entry["registered_yen"] == 1100
    assert entry["invested_yen"] == 100
    assert entry["returned_yen"] == 300
    assert entry["roi"] == 3.0


def test_purchase_history_can_be_aggregated_across_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(purchases.config, "PRESET_WEIGHTS_PATH", tmp_path / "preset.json")
    monkeypatch.setattr(purchases.payouts, "for_race", lambda _race: [{
        "type": "tan", "label": "単勝", "combo": [7], "text": "7",
        "payout_yen_per_100": 200, "popularity": 1,
    }])
    races = [
        {"race_id": "R1", "date": "20260801", "race_num": "01",
         "start_time": "10:00", "horses": [{"num": "07", "order": 1}]},
        {"race_id": "R2", "date": "20260808", "race_num": "01",
         "start_time": "10:00", "horses": [{"num": "07", "order": 1}]},
    ]
    ticket = [{"type": "tan", "label": "単勝", "combo": [7],
               "text": "7", "amount_yen": 100}]
    for race in races:
        row = purchases.record("alice", race=race, config_id=None, items=ticket,
                               now=purchases._start_at(race) - 60)
        purchases.confirm("alice", row["id"], now=purchases._start_at(race) - 30)

    assert purchases.recorded_dates("alice") == ["20260801", "20260808"]
    summary = purchases.list_for_dates("alice", ["20260801", "20260808"], races)
    assert summary["dates"] == ["20260801", "20260808"]
    assert summary["confirmed_races"] == 2
    assert summary["invested_yen"] == 200
    assert summary["returned_yen"] == 400
