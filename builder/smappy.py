"""JRAスマッピーへ買い目を送り、JRA生成の投票用QRを受け取る。

QRの投票データ自体はJRA側が ``pw_983_i.cgi`` で生成する。このモジュールは
スマッピーと同じ入力レコードを作って公式サーバへ送り、返された数字列を通常の
QR画像へ描画する。投票・購入を行うAPIではない。生成したQRを発売機にかざし、
発売機側で内容を確認して初めて購入になる。

スマッピーには公開APIが無いため、画面の動的トークンとCookieを毎回取得する。
公式画面が変わったときは推測で続行せず、構造不一致として停止する。
"""

from __future__ import annotations

import base64
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

BASE_URL = "https://qrcode.jra.go.jp/"
MAX_POINTS = 50
MAX_TOTAL_YEN = 1_000_000
MAX_RESPONSE = 2_000_000

SIKI = {
    "tan": 1,
    "fuku": 2,
    "wakuren": 3,
    "umaren": 4,
    "wide": 5,
    "umatan": 6,
    "sanrenpuku": 7,
    "sanrentan": 8,
}

_UNORDERED = {"wakuren", "umaren", "wide", "sanrenpuku"}
_LABELS = {
    "tan": "単勝", "fuku": "複勝", "wakuren": "枠連", "umaren": "馬連",
    "wide": "ワイド", "umatan": "馬単", "sanrenpuku": "三連複", "sanrentan": "三連単",
}


class SmappyError(RuntimeError):
    """安全に利用者へ返せるスマッピー連携エラー。"""

    def __init__(self, code: str, message: str, *, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Point:
    bet_type: str
    combo: tuple[int, ...]
    amount_yen: int


def _normalize_point(point: Point) -> Point:
    """JRAの通常買い1点形式へ合わせ、順不同券種だけを番号順にする。

    UI側の組み立ても同じ規則だが、実資金に使うQRの境界でも必ず検査する。
    馬連・ワイド・三連系の同じ馬の重複は成立しない。枠連の同枠だけは、同じ枠に
    2頭以上いる場合に成立するためここでは許可し、レース固有の検査は公式へ委ねる。
    """
    combo = tuple(int(x) for x in point.combo)
    if point.bet_type in _UNORDERED:
        combo = tuple(sorted(combo))
    if point.bet_type != "wakuren" and len(set(combo)) != len(combo):
        raise SmappyError("invalid_selection", "同じ馬番を重ねた買い目は送信できません", status=400)
    return Point(point.bet_type, combo, point.amount_yen)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self._current: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {str(k).lower(): ("" if v is None else str(v)) for k, v in attrs}
        if tag.lower() == "form":
            self._current = a.get("name")
            if self._current:
                self.forms.setdefault(self._current, {})
        elif tag.lower() == "input" and self._current:
            name = a.get("name")
            if name:
                self.forms[self._current][name] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def _form(html: str, name: str) -> dict[str, str]:
    p = _FormParser()
    p.feed(html)
    got = p.forms.get(name)
    if not got:
        raise SmappyError("official_page_changed",
                          f"JRA公式画面の送信フォーム({name})を確認できませんでした")
    return dict(got)


class _Http:
    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, path: str, data: dict[str, str] | None = None) -> str:
        url = urljoin(BASE_URL, path)
        body = urlencode(data).encode("ascii") if data is not None else None
        req = Request(url, data=body, headers={
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 Mobile/15E148"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Referer": BASE_URL,
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                raw = res.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise SmappyError("official_response_too_large",
                                      "JRA公式サイトから想定外の大きな応答がありました")
                charset = res.headers.get_content_charset() or "euc-jp"
                return raw.decode(charset, errors="replace")
        except SmappyError:
            raise
        except HTTPError as exc:
            raise SmappyError("official_http_error",
                              f"JRA公式サイトがHTTP {exc.code}を返しました") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SmappyError("official_unreachable",
                              "JRA公式サイトに接続できませんでした。通信環境を確認してください") from exc


def _mg_rows(html: str) -> list[str]:
    m = re.search(r"\bMg\s*=\s*new\s+Array\(([^;]+)\);", html)
    if not m:
        raise SmappyError("official_page_changed", "JRAの開催情報を読み取れませんでした")
    return re.findall(r'["\']([0-9A-F]+)["\']', m.group(1), flags=re.I)


def _day_code(date: str) -> str:
    try:
        # Python: 月=0..日=6 / スマッピー: 日=1, 月=2, ... 土=7
        return str(((datetime.strptime(date, "%Y%m%d").weekday() + 1) % 7) + 1)
    except ValueError as exc:
        raise SmappyError("invalid_race", "レース日付が正しくありません", status=400) from exc


def _today_jst() -> Date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def _require_current_sale_date(raw: str) -> None:
    """曜日だけが同じ過去レースを、現在の発売レースとして送らないためのゲート。

    スマッピーの1点レコードには年月日が入らず、開催日の曜日だけが入る。公式セッションが
    現在の発売日へ解決するため、過去データを開いたまま送ると別日の同競馬場・同Rに
    化けうる。通常の週末と3日開催を覆う「JSTの今日から3日先」だけを許可する。
    """
    try:
        target = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise SmappyError("invalid_race", "レース日付が正しくありません", status=400) from exc
    today = _today_jst()
    if target < today or target > today + timedelta(days=3):
        raise SmappyError(
            "race_not_on_sale",
            "過去日または発売期間外の日付はスマッピーQRにできません。現在の開催日を選んでください",
            status=409,
        )


def _meeting(html: str, date: str, track_code: str, race_num: int) -> tuple[str, str]:
    _require_current_sale_date(date)
    try:
        track_hex = f"{int(track_code):X}"
    except (TypeError, ValueError) as exc:
        raise SmappyError("invalid_race", "競馬場コードが正しくありません", status=400) from exc
    day = _day_code(date)
    rows = _mg_rows(html)
    idx = next((i for i, row in enumerate(rows)
                if len(row) >= 3 and row[1].upper() == track_hex and row[2] == day), None)
    if idx is None:
        raise SmappyError(
            "race_not_on_sale",
            "選択レースは現在スマッピーの発売対象にありません。受付時間も確認してください",
            status=409,
        )
    if not (1 <= race_num <= 12):
        raise SmappyError("invalid_race", "レース番号が正しくありません", status=400)
    # 00... は非発売。Jg の構造が変わった場合は公式サーバの検査へ委ねる。
    jm = re.search(rf"Jg\[{idx}\]\[{race_num - 1}\]\s*=\s*[\"']([^\"']+)", html)
    if jm and set(jm.group(1)) == {"0"}:
        raise SmappyError("race_not_on_sale", "選択レースは発売されていません", status=409)
    return track_hex, day


def _horse_mask(num: int) -> int:
    if not 1 <= num <= 18:
        raise SmappyError("invalid_selection", f"馬番{num}はスマッピーへ送信できません", status=400)
    # 1番=bit19、18番=bit2。bit20 はスマッピーの固定フラグ。
    return 0x100000 | (1 << (20 - num))


def _frame_upper(num: int) -> int:
    if not 1 <= num <= 8:
        raise SmappyError("invalid_selection", f"枠番{num}はスマッピーへ送信できません", status=400)
    return 0x100 | (1 << (8 - num))


def _frame_lower(num: int) -> int:
    if not 1 <= num <= 8:
        raise SmappyError("invalid_selection", f"枠番{num}はスマッピーへ送信できません", status=400)
    # JRAスクリプトの2枠目は bit9..2、bit12 が固定フラグ。
    return 0x1000 | (1 << (10 - num))


def _pair_mask(first: int, second: int) -> int:
    # 1頭目=bit35..18、2頭目=bit17..0、bit36 は固定フラグ。
    _horse_mask(first)
    _horse_mask(second)
    return 0x1000000000 | (1 << (36 - first)) | (1 << (18 - second))


def _bet_code(point: Point) -> str:
    siki = SIKI.get(point.bet_type)
    if not siki:
        raise SmappyError("invalid_selection",
                          f"{point.bet_type}はスマッピーへ送信できません", status=400)
    c = point.combo
    expected = 1 if siki in (1, 2) else (2 if siki in (3, 4, 5, 6) else 3)
    if len(c) != expected:
        raise SmappyError("invalid_selection", "買い目の頭数が券種と一致しません", status=400)

    if siki in (1, 2):
        data = f"{_horse_mask(c[0]) & 0xFFFFF:05x}" + "0" * 9
    elif siki == 3:
        data = (f"{_frame_upper(c[0]) & 0xFF:02x}" + "00"
                + f"{_frame_lower(c[1]) & 0xFFF:03x}" + "0" * 7)
    elif siki in (4, 5, 6):
        data = f"{_pair_mask(c[0], c[1]) & 0xFFFFFFFFF:09x}" + "0" * 5
    else:
        data = (f"{_pair_mask(c[0], c[1]) & 0xFFFFFFFFF:09x}"
                + f"{_horse_mask(c[2]) & 0xFFFFF:05x}")
    return f"0{siki}{data}".upper()


def _record(point: Point, *, index: int, track_hex: str,
            day_code: str, race_num: int) -> str:
    if point.amount_yen < 100 or point.amount_yen % 100:
        raise SmappyError("invalid_amount", "金額は100円単位で入力してください", status=400)
    units = point.amount_yen // 100
    if units > 0xFFFF:
        raise SmappyError("invalid_amount", "1点あたりの金額が上限を超えています", status=400)
    race_hex = f"{race_num:X}"
    raw = f"10{index:02d}{track_hex}{race_hex}{day_code}{_bet_code(point)}{units:04X}"
    if len(raw) != 27:
        raise SmappyError("official_format_error", "スマッピー送信データを作成できませんでした")
    return raw


def _qr_payload(html: str) -> str:
    m = re.search(r"\bQr\s*=\s*[\"']([0-9]+)[\"']\s*;", html)
    if not m:
        raise SmappyError("official_rejected",
                          "JRAがQRデータを返しませんでした。発売状況と買い目を確認してください",
                          status=409)
    payload = m.group(1)
    if len(payload) > 2000 or payload[0] not in "147":
        raise SmappyError("official_format_error", "JRAから想定外のQRデータが返されました")
    return payload


def _returned_records(html: str) -> list[str]:
    pairs = [(int(i), v) for i, v in re.findall(r"Nb\[(\d+)\]\s*=\s*[\"']([^\"']+)", html)]
    return [v for _, v in sorted(pairs) if v != "0"]


def _record_signature(record: str) -> str:
    """表示順の管理番号を除き、レース・券種・組番・金額だけを照合する。"""
    if len(record) != 27:
        return record
    return record[:2] + record[4:]


def _point_text(point: Point) -> str:
    arrow = "→" if point.bet_type in ("umatan", "sanrentan") else "-"
    return f"{_LABELS.get(point.bet_type, point.bet_type)} {arrow.join(map(str, point.combo))}"


def _png_data_url(payload: str) -> str:
    try:
        import qrcode
        from qrcode import base as qr_base
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise SmappyError("qr_renderer_missing",
                          "QR描画ライブラリがありません。requirements.txtを再インストールしてください") from exc
    # qrcode 8.2 は、Reed-Solomon の剰余が全て0になる正当なデータで
    # Polynomial([0]) を再帰処理し glog(0) を送出する。JRAの数字列で実際に発生した。
    # 0剰余は誤り訂正コードも全て0なので、その時点で返すのが数学的にも正しい。
    def zero_safe_mod(self, other):
        difference = len(self) - len(other)
        if difference < 0:
            return self
        if not self[0]:
            return qr_base.Polynomial([0], 0)
        ratio = qr_base.glog(self[0]) - qr_base.glog(other[0])
        num = [item ^ (qr_base.gexp(qr_base.glog(other_item) + ratio)
                       if other_item else 0)
               for item, other_item in zip(self, other)]
        if difference:
            num.extend(self[-difference:])
        if not any(num):
            return qr_base.Polynomial([0], 0)
        return qr_base.Polynomial(num, 0) % other

    if not getattr(qr_base.Polynomial.__mod__, "_maib_zero_safe", False):
        zero_safe_mod._maib_zero_safe = True
        qr_base.Polynomial.__mod__ = zero_safe_mod

    version = {"1": 14, "4": 5, "7": 10}[payload[0]]
    qr = qrcode.QRCode(version=version, error_correction=ERROR_CORRECT_M,
                       box_size=5, border=4)
    qr.add_data(payload, optimize=0)
    try:
        qr.make(fit=False)
    except (ValueError, OverflowError) as exc:
        raise SmappyError("official_format_error", "JRAのQRデータを画像化できませんでした") from exc
    out = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def create_qr(*, date: str, track_code: str, race_num: int,
              points: list[Point], http: _Http | None = None) -> dict:
    """公式スマッピーへ1レース分を送り、表示用QRと照合情報を返す。"""
    if not points:
        raise SmappyError("empty_selection", "買い目を1点以上選んでください", status=400)
    if len(points) > MAX_POINTS:
        raise SmappyError("too_many_points",
                           f"QR1件の上限は{MAX_POINTS}点です(現在{len(points)}点)", status=400)
    points = [_normalize_point(point) for point in points]
    total_yen = sum(p.amount_yen for p in points)
    if total_yen > MAX_TOTAL_YEN:
        raise SmappyError("amount_limit",
                          "合計金額がスマッピーの上限100万円を超えています", status=400)

    client = http or _Http()
    landing = client.request("")
    if "受付時間外" in landing:
        raise SmappyError("outside_hours", "現在はスマッピーのQR作成時間外です", status=409)
    bet_page = client.request("pw_982_i.cgi", _form(landing, "FORM0"))
    track_hex, day = _meeting(bet_page, date, track_code, race_num)
    send = _form(bet_page, "s")
    records = [_record(p, index=i, track_hex=track_hex, day_code=day,
                       race_num=race_num) for i, p in enumerate(points)]
    for i in range(MAX_POINTS):
        send[f"{i + 1:02d}"] = records[i] if i < len(records) else "0"

    result_page = client.request("pw_983_i.cgi", send)
    returned = _returned_records(result_page)
    if not returned:
        raise SmappyError(
            "race_not_on_sale",
            "JRAでこのレースの買い目を受け付けられませんでした。締切時刻・発売状況・出走取消を確認してください",
            status=409,
        )
    sent_signatures = Counter(_record_signature(record) for record in records)
    returned_signatures = Counter(_record_signature(record) for record in returned)
    if returned_signatures != sent_signatures:
        accepted = returned_signatures.copy()
        rejected_points = []
        for point, record in zip(points, records):
            signature = _record_signature(record)
            if accepted[signature] > 0:
                accepted[signature] -= 1
            else:
                rejected_points.append(_point_text(point))
        rejected = max(1, len(rejected_points))
        detail = "・".join(rejected_points[:3])
        suffix = f"（{detail}）" if detail else ""
        raise SmappyError(
            "official_mismatch",
            f"JRAで{rejected}点が受付対象外になったためQRを作成していません{suffix}。"
            "出走取消・発売状況・買い目を確認してください",
            status=409,
        )
    payload = _qr_payload(result_page)
    stamp = payload[2:16]
    created_at = None
    if len(stamp) == 14 and stamp.isdigit():
        try:
            created_at = datetime.strptime(stamp, "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    return {
        "qr_png": _png_data_url(payload),
        "created_at": created_at,
        "points": len(points),
        "total_yen": total_yen,
        "verified": True,
    }
