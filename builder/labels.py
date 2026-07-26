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


# ---------------------------------------------------------------------------
# 用語辞書 — 競馬を知らない人が読んで意味が通ることを基準に書く
# ---------------------------------------------------------------------------
# UI 側に説明文を複製しない (語彙の二重管理を禁止)。API 経由で供給する。
# 「儲かる」「買い目」等の語彙は入れない。
GLOSSARY: dict[str, dict[str, str]] = {
    "mark": {"term": "印", "reading": "しるし",
             "desc": "予想の順位づけです。◎本命 ○対抗 ▲単穴 △連下 ×注意 の順に有力とみています。"
                     "マイAIが評価した上位5頭に付きます。"},
    "honmei": {"term": "◎ 本命", "desc": "そのレースで最も有力とみた1頭です。"},
    "taikou": {"term": "○ 対抗", "desc": "本命に次いで有力とみた1頭です。"},
    "tanana": {"term": "▲ 単穴", "desc": "3番目の評価。上位2頭を逆転する可能性をみています。"},
    "renka": {"term": "△ 連下", "desc": "4番目の評価。3着以内に入る可能性をみています。"},
    "chuui": {"term": "× 注意", "desc": "5番目の評価。押さえておきたい1頭です。"},
    "mujirushi": {"term": "– 無印", "desc": "6番目以降の評価です。印は上位5頭までなので付きません。"},
    "win_odds": {"term": "単勝オッズ", "reading": "たんしょうオッズ",
                 "desc": "その馬が1着になった場合の払戻倍率です。低いほど多くの人が支持しています。"},
    "popularity": {"term": "人気", "desc": "単勝オッズの低い順に付けた順位です。"
                                          "1番人気が最も支持されている馬です。"},
    "fukushou": {"term": "複勝", "reading": "ふくしょう",
                 "desc": "3着以内に入ることです。「複勝率」は3着以内に入った割合です。"},
    "condition": {"term": "馬場状態", "reading": "ばばじょうたい",
                  "desc": "コースの湿り具合です。乾いている順に 良・稍重・重・不良 の4段階。"
                          "馬によって得意な状態が違います。"},
    "burden_weight": {"term": "斤量", "reading": "きんりょう",
                      "desc": "その馬が背負う重さ(騎手+装具)です。重いほど不利とされます。"},
    "horse_weight": {"term": "馬体重", "desc": "馬の体重です。前走からの増減が調子の目安になります。"},
    "time_index": {"term": "タイム指数",
                   "desc": "走破時計を距離で割って比較できるようにした指標です。"
                           "距離の違うレースのタイムを並べて見るために使います。"},
    "final_3f": {"term": "上がり3F", "reading": "あがりスリーエフ",
                 "desc": "最後の600メートル(3ハロン)にかかった時間です。短いほど終盤に伸びています。"},
    "corner": {"term": "コーナー通過順位",
               "desc": "各コーナーを何番手で回ったかです。前で運んだか後方から追い込んだかが分かります。"},
    "last_corner": {"term": "最終コーナー", "reading": "さいしゅうコーナー",
                    "desc": "直線に入る直前のコーナーです。ここでの位置どりが結果に影響します。"},
    "margin": {"term": "着差", "reading": "ちゃくさ",
               "desc": "勝ち馬との差です。小さいほど惜しい負け方をしています。"},
    "prize": {"term": "獲得本賞金", "reading": "かくとくほんしょうきん",
              "desc": "これまでに獲得した賞金の本体部分です(付加賞・褒賞金は含みません)。"
                      "強い相手と戦ってきたかの目安になります。"},
    "surface": {"term": "芝・ダート",
                "desc": "コースの種類です。芝は草、ダートは砂。得意な方が馬によって違います。"},
    "draw": {"term": "枠位置", "reading": "わくいち",
             "desc": "ゲートの並び順です。内側は距離のロスが少なく、外側は他馬の影響を受けにくいとされます。"},
    "sire": {"term": "父", "desc": "その馬の父馬です。父の得意条件が仔にも出ることがあります。"},
    "dam_sire": {"term": "母父", "reading": "ははちち",
                 "desc": "母の父です。父とは別の傾向を仔に伝えることがあります。"},
    "confidence": {"term": "自信度",
                   "desc": "1位と2位の評価差から3段階で示しています。"
                           "差が大きいほど鉄板級、小さいほど混戦です。"},
    "upset": {"term": "人気を出し抜いた的中",
              "desc": "1番人気ではない馬を◎にして、その馬が1着になったレースです。"},
    "backtest": {"term": "これまでの成績",
                 "desc": "過去のレースに同じ設定を当てはめて集計した的中率です。"
                         "学習に使っていない期間で計算しています。"},
    "coverage": {"term": "分析に使えた項目",
                 "desc": "選んだ項目のうち、そのレースで実際に評価に使えた数です。"
                         "データが足りない項目は評価から外します。"},
    "low_sample": {"term": "データ少なめ",
                   "desc": "この項目は記録の保存期間が短く、他の項目より根拠が薄くなります。"},
}


# 成績比較の並び順 (判断C で確定)。**静的な文字列としてここに置く。**
# API レスポンス経由で埋めていたため、board が空だと差し込まれず画面に
# プレースホルダの「—」が残っていた。規則は集計結果に依存しない事実なので、
# 語彙表の一部として持ち、選択肢と同じ経路 (/api/features) で常に供給する。
RANKING_RULE = ("並び順は ①◎的中数 → ②人気を出し抜いた的中数 → ③◎複勝率 "
                "の順です。それでも同じなら同順位で並びます。")


def glossary() -> list[dict]:
    """用語辞書を API で返せる形にする (キー順で安定)。"""
    return [{"key": k, **v} for k, v in GLOSSARY.items()]


# ---------------------------------------------------------------------------
# STEP1 の自然語ラベルとグループ (設計: 初心者が読んで意味が通ること)
# ---------------------------------------------------------------------------
# 開発者語彙と「×」記法を画面から全廃する。特徴量キーは変更しない。
# 3要素: (自然語ラベル, グループ, 用語辞書のキー | None)
_STEP1: dict[str, tuple[str, str, str | None]] = {
    "popularity": ("人気(市場)", "market", "popularity"),

    "burden_weight": ("斤量", "condition", "burden_weight"),
    "burden_delta": ("前走からの斤量の増減", "condition", "burden_weight"),
    "horse_weight_change": ("前走からの馬体重の増減", "condition", "horse_weight"),
    "days_since_last": ("前走からの間隔", "condition", None),

    "jockey_win_rate": ("騎手の勝率", "people", None),
    "jockey_recent_30d_top3_rate": ("騎手の最近30日の成績", "people", "fukushou"),
    "jockey_track_top3_rate": ("騎手のこの競馬場での成績", "people", "fukushou"),
    "trainer_win_rate": ("調教師の勝率", "people", None),
    "trainer_recent_30d_top3_rate": ("調教師の最近30日の成績", "people", "fukushou"),

    "sire_surface_top3_rate": ("父の芝ダート適性", "blood", "sire"),
    "sire_distance_top3_rate": ("父の距離適性", "blood", "sire"),
    "sire_going_top3_rate": ("父の馬場状態適性", "blood", "condition"),
    "dam_sire_surface_top3_rate": ("母父の芝ダート適性", "blood", "dam_sire"),

    "fit_course": ("このコースでの実績", "record", "fukushou"),
    "fit_course_distance": ("このコースと距離での実績", "record", "fukushou"),
    "fit_distance": ("この距離での実績", "record", "fukushou"),
    "fit_going": ("この馬場状態での実績", "record", "condition"),
    "fit_surface": ("芝ダートの適性", "record", "surface"),
    "horse_track_top3_rate": ("この競馬場での実績", "record", "fukushou"),
    "horse_recent_90d_top3_rate": ("最近90日の実績", "record", "fukushou"),
    "recent_avg_finish": ("近走の平均着順", "record", None),
    "recent_trend_delta": ("近走の調子", "record", None),
    "last_finish": ("前走の着順", "record", None),

    "draw_position": ("枠の内外", "running", "draw"),
    "avg_final_3f": ("終盤の脚(上がり3F)", "running", "final_3f"),
    "best_final_3f_rank": ("終盤の脚の最高順位", "running", "final_3f"),
    "recent_4corner_avg_position": ("最終コーナーでの位置どり", "running", "last_corner"),
    "recent_4corner_position_change": ("コーナーでの押し上げ", "running", "corner"),
}

# グループの表示名と1行説明 (順序が画面の並び順になる)
STEP1_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("condition", "馬の状態", "斤量や馬体重の増減など、当日のコンディションをみます"),
    ("record", "過去の実績", "同じ条件のレースでどれだけ走れているかをみます"),
    ("people", "騎手・調教師", "乗る人・仕上げる人の成績をみます"),
    ("running", "脚質・展開", "位置どりや終盤の伸びなど、走り方の傾向をみます"),
    ("blood", "血統", "父・母父の得意条件との一致度をみます"),
    ("market", "市場", "オッズに現れた支持をみます"),
)


def step1_label(key: str) -> str:
    """STEP1 の自然語ラベル。定義が無ければ FEATURES の名前に落とす。"""
    got = _STEP1.get(key)
    if got:
        return got[0]
    from . import model
    feat = model.FEATURES.get(key)
    return feat.label if feat else key


def step1_group(key: str) -> str:
    got = _STEP1.get(key)
    return got[1] if got else "other"


def glossary_key(key: str) -> str | None:
    """その項目に紐づく用語辞書のキー (無ければ None)。"""
    got = _STEP1.get(key)
    return got[2] if got else None


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
    if feat.kind != "aggregate":
        # STEP1 は自然語ラベル (「父×芝ダート」のような開発者記法を画面に出さない)
        return step1_label(key)
    base = feat.label.replace("(可変集計)", "")
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
