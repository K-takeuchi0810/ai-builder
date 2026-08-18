"""JRAスマッピー連携。ネットワークを使わない合成応答でプロトコルを固定する。"""

from __future__ import annotations

import pytest

from builder import smappy


def test_one_win_record_matches_the_official_javascript_encoding():
    point = smappy.Point("tan", (1,), 100)
    got = smappy._record(point, index=0, track_hex="4", day_code="7", race_num=1)
    # 2026-08-01 新潟1R・単勝01・100円でJRA公式画面が受け付けた値。
    assert got == "100041701800000000000000001"
    assert len(got) == 27


@pytest.mark.parametrize(("kind", "combo", "code"), [
    ("tan", (1,), "0180000000000000"),
    ("fuku", (18,), "0200004000000000"),
    ("umatan", (11, 10), "0600200010000000"),
    ("sanrentan", (11, 10, 6), "0800200010004000"),
    ("wakuren", (1, 8), "0380000040000000"),
])
def test_each_normal_bet_code(kind, combo, code):
    assert smappy._bet_code(smappy.Point(kind, combo, 100)) == code


@pytest.mark.parametrize(("kind", "combo", "want"), [
    ("umaren", (8, 2), (2, 8)),
    ("wide", (11, 1), (1, 11)),
    ("sanrenpuku", (9, 2, 5), (2, 5, 9)),
    ("wakuren", (7, 3), (3, 7)),
    ("umatan", (8, 2), (8, 2)),
])
def test_normalize_point_sorts_only_unordered_bets(kind, combo, want):
    assert smappy._normalize_point(smappy.Point(kind, combo, 100)).combo == want


def test_normalize_point_rejects_the_same_horse_except_wakuren_zoro():
    with pytest.raises(smappy.SmappyError) as exc:
        smappy._normalize_point(smappy.Point("umaren", (2, 2), 100))
    assert exc.value.code == "invalid_selection"
    assert smappy._normalize_point(smappy.Point("wakuren", (3, 3), 100)).combo == (3, 3)


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, path, data=None):
        self.calls.append((path, data))
        if path == "":
            return ('<form name="FORM0"><input name="uh" value="a">'
                    '<input name="g" value="980"></form>')
        if path == "pw_982_i.cgi":
            inputs = "".join(f'<input name="{i:02d}" value="0">' for i in range(1, 51))
            return ("<script>Mg = new Array(\"A470203222\");"
                    "Jg=new Array(); Jg[0]=new Array();"
                    "Jg[0][0]=\"0949110FFFFFF003FFFFFFC000\";</script>"
                    f'<form name="s">{inputs}<input name="uh" value="b"></form>')
        if path == "pw_983_i.cgi":
            records = [data[f"{i:02d}"] for i in range(1, 51)
                       if data[f"{i:02d}"] != "0"]
            nb = "\n".join(f'Nb[{i}] = "{v}";' for i, v in enumerate(records))
            return nb + '\nQr = "412026073122001600000000000000000000000000000000";'
        raise AssertionError(path)


def test_create_qr_posts_to_jra_and_requires_the_echo_to_match(monkeypatch):
    monkeypatch.setattr(smappy, "_png_data_url", lambda payload: "data:image/png;base64,TEST")
    monkeypatch.setattr(smappy, "_today_jst", lambda: smappy.Date(2026, 7, 31))
    http = FakeHttp()
    got = smappy.create_qr(
        date="20260801", track_code="04", race_num=1,
        points=[smappy.Point("tan", (1,), 100)], http=http)
    assert got["verified"] is True
    assert got["points"] == 1 and got["total_yen"] == 100
    assert got["qr_png"].startswith("data:image/png")
    sent = http.calls[-1][1]
    assert sent["01"] == "100041701800000000000000001"
    assert sent["02"] == "0" and sent["50"] == "0"


def test_create_qr_normalizes_an_unordered_combo_before_posting(monkeypatch):
    monkeypatch.setattr(smappy, "_png_data_url", lambda payload: "data:image/png;base64,TEST")
    monkeypatch.setattr(smappy, "_today_jst", lambda: smappy.Date(2026, 7, 31))
    http = FakeHttp()
    smappy.create_qr(
        date="20260801", track_code="04", race_num=1,
        points=[smappy.Point("umaren", (8, 2), 100)], http=http)
    sent_record = http.calls[-1][1]["01"]
    assert sent_record == smappy._record(smappy.Point("umaren", (2, 8), 100),
                                          index=0, track_hex="4", day_code="7", race_num=1)


def test_create_qr_reports_a_closed_or_unavailable_race(monkeypatch):
    monkeypatch.setattr(smappy, "_today_jst", lambda: smappy.Date(2026, 7, 31))
    http = FakeHttp()
    original = http.request

    def request(path, data=None):
        if path == "pw_983_i.cgi":
            return 'Qr = "412026073122001600000000000000000000000000000000";'
        return original(path, data)

    http.request = request
    with pytest.raises(smappy.SmappyError) as exc:
        smappy.create_qr(
            date="20260801", track_code="04", race_num=1,
            points=[smappy.Point("tan", (1,), 100)], http=http)
    assert exc.value.code == "race_not_on_sale"
    assert "締切" in exc.value.message


def test_create_qr_accepts_official_reordering_when_every_ticket_still_matches(monkeypatch):
    monkeypatch.setattr(smappy, "_png_data_url", lambda payload: "data:image/png;base64,TEST")
    monkeypatch.setattr(smappy, "_today_jst", lambda: smappy.Date(2026, 7, 31))
    http = FakeHttp()
    original = http.request

    def request(path, data=None):
        if path != "pw_983_i.cgi":
            return original(path, data)
        first, second = data["01"], data["02"]
        second = second[:2] + "00" + second[4:]
        first = first[:2] + "01" + first[4:]
        return (f'Nb[0] = "{second}"; Nb[1] = "{first}"; '
                'Qr = "412026073122001600000000000000000000000000000000";')

    http.request = request
    got = smappy.create_qr(
        date="20260801", track_code="04", race_num=1,
        points=[smappy.Point("umaren", (2, 8), 100),
                smappy.Point("tan", (1,), 200)], http=http)
    assert got["verified"] is True and got["points"] == 2


def test_qr_renderer_handles_a_valid_zero_remainder_payload():
    # 実際にJRAが3点へ返した数字列。qrcode 8.2ではRS剰余が全0になり glog(0) だった。
    payload = (
        "72202608021008480000000000000000000002430107260204080202000001006250002000000000"
        "00000000000000000005013500004000000000000000000000000000000000000001072602040802"
        "02000008006780003000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000000000000000000000000000002463292802100848"
        "0000"
    )
    assert len(payload) == 484
    assert smappy._png_data_url(payload).startswith("data:image/png;base64,")


def test_create_qr_rejects_more_than_fifty_points():
    points = [smappy.Point("tan", (1,), 100)] * 51
    with pytest.raises(smappy.SmappyError) as exc:
        smappy.create_qr(date="20260801", track_code="04", race_num=1, points=points)
    assert exc.value.code == "too_many_points"


def test_create_qr_rejects_non_100_yen_amounts():
    with pytest.raises(smappy.SmappyError) as exc:
        smappy._record(smappy.Point("tan", (1,), 150), index=0,
                        track_hex="4", day_code="7", race_num=1)
    assert exc.value.code == "invalid_amount"


def test_historical_same_weekday_cannot_turn_into_the_current_race(monkeypatch):
    monkeypatch.setattr(smappy, "_today_jst", lambda: smappy.Date(2026, 7, 31))
    with pytest.raises(smappy.SmappyError) as exc:
        smappy._require_current_sale_date("20260725")
    assert exc.value.code == "race_not_on_sale"
