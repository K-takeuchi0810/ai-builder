"""ai-builder ↔ keiba-yosou の橋渡し。keiba-yosou を兄弟ディレクトリとして sys.path に
載せ、既存の予想ロジック・系統分類・レース列挙・払戻を **read-only** で再利用する。

重要な不変条件:
- keiba-yosou 側のファイルは一切変更しない (import して使うだけ)。
- 予想パイプライン (predictor/rules.py 等) は ai-builder を import しないので、
  ここでの利用が予想出力に影響することは構造的にない。
- DB は open_db_readonly で開く (書き込み・ロック競合なし)。

配線は keiba-yosou の scripts/bias_scan.py (「predict → セグメント別 calibration」の
実証済み参照実装) と同じ母集団・同じフィールドで組んである:
- predict_race(horses, conn=conn, race=race, cache=feature_cache) を使う。
  第1引数は出馬表 (horses_for_race の戻り) で、conn+race を渡すと過去走ベースの本格
  スコアリングになる。feature_cache は全レースで 1 個を共有する (bias_scan と同じ。
  cache key が (kind, blood, date, ...) で日付込みなのでレース跨ぎ共有は安全かつ高速)。
- calibration に使う確率は Prediction.raw_blended_probability (LGBM blend 直後・
  calibrator 適用前・race 内 Σ=1)。win_probability は投資確率で確率解釈不可なので使わない。
- is_tentative は **予測リスト全体を 1 レース 1 回**判定する per-race 関数。pick 母集団では
  暫定レースを丸ごとスキップする。
- pick は preds[0] (rank 1 = ◎ 本命)。bias_scan と同一定義にして global_train が
  bias_scan の全体 calibration と一致するようにする。
- 払戻は payouts テーブルの行 (get_payout_row) をレース単位で 1 回引き、馬ごとに
  payout_from_row(..., "tan") で 100 円賭けあたりの払戻円 (int) を得る。外れ・行欠損は 0。
  単位払戻率は payout/100.0。
- odds の信頼性ゲートは race_odds_untrusted(horses, race, max_snapshot_age_min)。
  歴史的確定オッズ (odds_fetched_at が全馬 NULL) は untrusted=False (信頼)。
- horses_for_race は horse_races のみで sire_name/dam_sire_name を含まないため、
  blood_register_num で horse_masters を read-only join して系統名を得る。
"""

from __future__ import annotations

import sqlite3
import sys

from . import axes as ax
from . import config
from .explore import Sample


def _ensure_keiba_on_path() -> None:
    p = str(config.KEIBA_YOSOU_PATH)
    if p not in sys.path:
        sys.path.insert(0, p)


def open_conn():
    """keiba-yosou の read-only 接続 (context manager) を返す。"""
    _ensure_keiba_on_path()
    from db import open_db_readonly  # type: ignore
    return open_db_readonly(str(config.KEIBA_DB_PATH))


# web.codes.track_type は日本語ラベル (芝/ダート/障害) を返すので、探索軸で使う英字キーに
# 変換する。bias_scan.surface_key と同一のマッピングにして surface/condition 軸を一致させる
# (これを怠ると surface が日本語のまま漏れ、axes.condition_key の surface=="turf" 分岐が
#  芝レースで永久に外れて condition が全て unknown に潰れる)。
_SURFACE_JP_TO_EN = {"芝": "turf", "ダート": "dirt", "障害": "jump"}


def _surface_of(track_type_code: str | None) -> str:
    """track_type_code → turf/dirt/jump/other。keiba-yosou の web.codes.track_type に委譲。"""
    _ensure_keiba_on_path()
    try:
        from web.codes import track_type  # type: ignore
        return _SURFACE_JP_TO_EN.get(track_type(track_type_code), "other")
    except Exception:  # noqa: BLE001 — 解決不能なら other 扱い
        return "other"


def _classify(conn, sire_name: str | None, breeding_num: str | None):
    """種牡馬名 (+任意で breeding_num, conn) → (line_short, country)。

    keiba-yosou の webapp/aggregate.py と同じ呼び方で classify_sire に conn と
    sire_breeding_num を渡し、名前直照合で当たらない馬も breeding_horses 遡上で
    分類できるようにする。名前も番号も無ければ unknown に誠実に落ちる。
    """
    _ensure_keiba_on_path()
    from predictor import sire_lines as sl  # type: ignore
    k = sl.classify_sire(sire_name, conn=conn, sire_breeding_num=breeding_num)
    return sl.line_label_short(k), sl.country_label(sl.classify_country(sire_name, k))


def _sires(conn, blood_register_num: str | None):
    """blood_register_num → (sire_name, sire_bn, dam_sire_name, dam_sire_bn)。

    horses_for_race は horse_races のみで血統名を持たないため horse_masters を read-only で引く。
    行が無ければ全 None (→ _classify が unknown)。
    """
    if not blood_register_num:
        return (None, None, None, None)
    try:
        row = conn.execute(
            "SELECT sire_name, sire_breeding_num, dam_sire_name, dam_sire_breeding_num "
            "FROM horse_masters WHERE blood_register_num=?",
            (blood_register_num,),
        ).fetchone()
    except sqlite3.OperationalError:
        # 3 代血統列を持たない旧 DB (migration 未適用) では列不在で落ちるため、
        # unknown へ縮退させて _classify/_surface_of の try/except と対称にする。
        return (None, None, None, None)
    if row is None:
        return (None, None, None, None)
    return (row["sire_name"], row["sire_breeding_num"],
            row["dam_sire_name"], row["dam_sire_breeding_num"])


def _race_predictions(conn, race: dict, horses: list[dict], feature_cache: dict):
    """1 レースを予測し (preds, tentative) を返す。

    preds は rank 昇順の list[Prediction] (dataclass; 属性アクセス)。tentative は
    そのレースが暫定予想か否か (is_tentative は per-race で 1 回だけ呼ぶ)。
    feature_cache は呼び出し側で全レース共有する 1 個の dict。
    """
    _ensure_keiba_on_path()
    from predictor.rules import is_tentative, predict_race  # type: ignore

    preds = predict_race(horses, conn=conn, race=race, cache=feature_cache)
    tentative = is_tentative(preds) if preds else False
    return preds, tentative


def build_samples(from_date: str, to_date: str, subject: str = "pick") -> list[Sample]:
    """実 DB から探索用 Sample を生成する。subject: 'pick'(◎=preds[0] のみ) or 'all'(全馬)。

    母集団定義は bias_scan.run_scan と同一:
      - list_races(jra_only=True, require_confirmed=True) で中央・確定レースのみ
      - 出走馬が無い / 確定 1 着が無いレースはスキップ
      - subject=='pick' の暫定レースは丸ごとスキップ
    """
    _ensure_keiba_on_path()
    from scripts.backtest import (  # type: ignore
        get_payout_row,
        horses_for_race,
        list_races,
        payout_from_row,
        popularity_config,
        race_odds_untrusted,
    )

    max_age = popularity_config().get("max_snapshot_age_min")
    feature_cache: dict = {}  # 全レース共有 (bias_scan と同じ。key が日付込みで安全)

    samples: list[Sample] = []
    with open_conn() as conn:
        races = list_races(conn, from_date, to_date, jra_only=True, require_confirmed=True)
        for race in races:
            horses = horses_for_race(conn, race)
            if not horses:
                continue
            if not any(h.get("confirmed_order") == 1 for h in horses):
                continue  # 確定勝ち馬が無いレースは母集団から除外 (bias_scan と同じ)
            preds, tentative = _race_predictions(conn, race, horses, feature_cache)
            if not preds:
                continue
            if subject == "pick" and tentative:
                continue

            horse_by_num = {str(h.get("horse_num")): h for h in horses}
            untrusted = race_odds_untrusted(horses, race, max_age)
            surface = _surface_of(race.get("track_type_code"))
            date = f"{race['race_year']}{race['race_month_day']}"
            # 払戻行はレース単位で 1 回だけ引く (subject=='all' の N+1 点引きを回避)。
            # オッズ不信頼レースは回収率を出さないので払戻自体を引かない。
            payout_row = None if untrusted else get_payout_row(conn, race)

            subject_preds = [preds[0]] if subject == "pick" else preds
            for p in subject_preds:
                hn = str(p.horse_num)
                h = horse_by_num.get(hn)
                if h is None:
                    continue
                co = h.get("confirmed_order")
                won = 1 if co == 1 else 0
                top3 = 1 if isinstance(co, int) and 1 <= co <= 3 else 0

                sire_name, sire_bn, dam_sire_name, dam_sire_bn = _sires(
                    conn, h.get("blood_register_num"))
                sire_line, sire_country = _classify(conn, sire_name, sire_bn)
                dam_line, dam_country = _classify(conn, dam_sire_name, dam_sire_bn)

                # 単位払戻率 (100 円賭け)。外れ・払戻行欠損は payout_from_row が 0 を返す。
                # オッズ不信頼なら回収率を出さない (None)。
                ret = None if untrusted else payout_from_row(payout_row, hn, "tan") / 100.0

                samples.append(Sample(
                    date=date,
                    axes=ax.derive_axes(
                        track=str(race.get("track_code")),
                        surface=surface,
                        distance=race.get("distance"),
                        turf_condition=race.get("turf_condition"),
                        dirt_condition=race.get("dirt_condition"),
                        weather_code=race.get("weather_code"),
                        race_month_day=race.get("race_month_day", ""),
                        kaiji=race.get("kaiji"),
                        nichiji=race.get("nichiji"),
                        popularity=h.get("win_popularity"),
                        sire_line=sire_line, sire_country=sire_country,
                        dam_sire_line=dam_line, dam_sire_country=dam_country,
                    ),
                    pred=float(p.raw_blended_probability),
                    won=won, top3=top3, ret=ret,
                ))
    return samples
