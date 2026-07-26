"""軸名・軸値の日本語ラベル化 (一般ユーザー向け表示のため)。

探索エンジン (explore/axes) は英字キーで軸値を持つが、UI では日本語で見せる。
競馬場名など keiba-yosou に既存のコード表があるものは web.codes に委譲し、
ai-builder 独自の bucket (距離帯・人気帯・馬場など) はここで日本語化する。
系統名 (sire_line 等) は keiba-yosou の line_label_short が既に日本語を返すので素通し。
"""

from __future__ import annotations

import sys

from . import config

# 軸そのものの表示名
AXIS_LABELS: dict[str, str] = {
    "track": "競馬場",
    "surface": "芝・ダート",
    "distance": "距離",
    "condition": "馬場",
    "weather": "天気",
    "weather_wet": "馬場の湿り",
    "meet_progress": "開催の進行",
    "kaiji": "開催回",
    "month": "月",
    "season": "季節",
    "popularity": "人気",
    "sire_line": "父系統",
    "sire_country": "父の系統タイプ",
    "dam_sire_line": "母父系統",
    "dam_sire_country": "母父の系統タイプ",
}

# ai-builder の bucket → 日本語 (axes.py の出力キーに対応)
_SURFACE = {"turf": "芝", "dirt": "ダート", "jump": "障害", "other": "その他"}
_DISTANCE = {"sprint": "短距離", "mile": "マイル", "middle": "中距離",
             "long": "長距離", "unknown": "不明"}
_CONDITION = {"firm": "良", "good": "稍重", "yielding": "重", "soft": "不良",
              "unknown": "不明"}
_WEATHER = {"clear": "晴", "cloudy": "曇", "light_rain": "小雨", "rain": "雨",
            "light_snow": "小雪", "snow": "雪", "unknown": "不明"}
_WEATHER_WET = {"dry": "乾いた馬場", "wet": "湿った馬場", "unknown": "不明"}
_MEET = {"early": "前半", "mid": "中盤", "late": "後半", "unknown": "不明"}
_SEASON = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬",
           "unknown": "不明"}
_POPULARITY = {"1": "1番人気", "2": "2番人気", "3": "3番人気", "4-6": "4〜6番人気",
               "7-9": "7〜9番人気", "10+": "10番人気以下", "unknown": "不明"}


# STEP2 のセル (一致条件 × さかのぼる範囲) のラベル。
#   選択肢用 = 参加者が選ぶときの言い方、短縮形 = 列名に埋め込むときの言い方。
# 両方をここに置き、API の選択肢生成と列ラベル生成の**どちらもここを参照する**
# (2箇所に別々の対応表を書くと片方だけ変わって静かにずれる)。
_MATCH_LABELS: dict[tuple[str, ...], tuple[str, str]] = {
    (): ("全レース", "全レース"),
    ("distance",): ("距離が同じ", "同距離"),
    ("track",): ("競馬場が同じ", "同競馬場"),
    ("surface",): ("芝ダートを分ける", "芝ダート別"),
}


def match_label(match, *, short: bool = False) -> str:
    """一致条件 (["distance"] 等) → 日本語。未知の組み合わせは軸名を並べる。"""
    key = tuple(sorted(match or []))
    got = _MATCH_LABELS.get(key)
    if got:
        return got[1] if short else got[0]
    return "・".join(axis_label(a) for a in key) or "全レース"


def lookback_label(lookback, *, short: bool = False) -> str:
    """さかのぼる範囲 (None=全走 / N=直近N走) → 日本語。"""
    if lookback in (None, "", 0):
        return "全走" if short else "全レース"
    return f"直近{int(lookback)}走" if short else f"直近{int(lookback)}レース"


def column_label(key: str, match=None, lookback=None) -> str:
    """基底列の表示名。集計列は **どのセルか** まで含める。

    設計書 §3.1 が要求する「タイム指数(同距離・直近3走) +2.1」という粒度の説明は、
    セルを含んだ列名がないと成立しない (同じ集計対象の別セルを複数選ぶと、
    寄与の行が全部同じ名前になって参加者が区別できなくなる)。
    """
    from . import model                                   # 循環 import を避けて遅延
    feat = model.FEATURES.get(key)
    if feat is None:
        return key
    base = feat.label.replace("(可変集計)", "")
    if feat.kind != "aggregate":
        return base
    return f"{base}({match_label(match, short=True)}・{lookback_label(lookback, short=True)})"


def _track_name(code: str) -> str:
    """競馬場コード → 名称。keiba-yosou の web.codes.track_name に委譲 (無ければコードのまま)。"""
    p = str(config.KEIBA_YOSOU_PATH)
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        from web.codes import track_name  # type: ignore
        return track_name(code) or code
    except Exception:  # noqa: BLE001
        return code


def value_label(axis: str, value: str) -> str:
    """軸 axis の値 value を日本語ラベルにする。未知の軸/値はそのまま返す (誠実に素通し)。"""
    v = "" if value is None else str(value)
    if axis == "track":
        return _track_name(v)
    table = {
        "surface": _SURFACE, "distance": _DISTANCE, "condition": _CONDITION,
        "weather": _WEATHER, "weather_wet": _WEATHER_WET, "meet_progress": _MEET,
        "season": _SEASON, "popularity": _POPULARITY,
    }.get(axis)
    if table is not None:
        return table.get(v, v)
    if axis == "kaiji":
        return f"{v}回" if v and v != "unknown" else "不明"
    if axis == "month":
        return f"{v}月" if v and v != "unknown" else "不明"
    if axis in ("sire_line", "sire_country", "dam_sire_line", "dam_sire_country"):
        # keiba-yosou の line_label_short/country_label は既に日本語。空/unknown だけ整える。
        return "不明" if v in ("", "unknown") else v
    return v if v else "不明"


def axis_label(axis: str) -> str:
    return AXIS_LABELS.get(axis, axis)
