"""keiba_bridge の配線テスト。

2 層に分ける:
- CI-safe: keiba-yosou も lightgbm も不要な純ロジック (_sires の SQL join)。
  keiba_bridge の import 自体は安全 (keiba-yosou への import は各関数内で遅延)。
- 実 DB 依存: keiba-yosou の DB + lightgbm が揃う環境でのみ実行 (それ以外は skip)。
  build_samples が実 predict_race を通して Sample を返すことの end-to-end 確認。
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import config                 # noqa: E402
from builder import keiba_bridge as kb      # noqa: E402


# ---------------------------------------------------------------------------
# CI-safe: _sires の horse_masters join
# ---------------------------------------------------------------------------
def _memory_conn_with_masters() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE horse_masters ("
        " blood_register_num TEXT PRIMARY KEY, sire_name TEXT, sire_breeding_num TEXT,"
        " dam_sire_name TEXT, dam_sire_breeding_num TEXT)"
    )
    conn.execute(
        "INSERT INTO horse_masters VALUES (?,?,?,?,?)",
        ("2020100001", "ディープインパクト", "S001", "Storm Cat", "D001"),
    )
    return conn


def test_sires_join_hit():
    conn = _memory_conn_with_masters()
    assert kb._sires(conn, "2020100001") == (
        "ディープインパクト", "S001", "Storm Cat", "D001")


def test_sires_join_miss_and_null():
    conn = _memory_conn_with_masters()
    # 未知の登録番号 → 全 None (unknown に誠実に落ちる)
    assert kb._sires(conn, "9999999999") == (None, None, None, None)
    # 空/None の登録番号 → SQL を打たず全 None
    assert kb._sires(conn, None) == (None, None, None, None)
    assert kb._sires(conn, "") == (None, None, None, None)


# ---------------------------------------------------------------------------
# 実 DB 依存: build_samples の end-to-end (lightgbm + keiba.db がある環境のみ)
# ---------------------------------------------------------------------------
def _real_env_ready() -> bool:
    if not config.KEIBA_DB_PATH.exists():
        return False
    kb._ensure_keiba_on_path()
    import importlib.util
    return importlib.util.find_spec("lightgbm") is not None


requires_real_db = pytest.mark.skipif(
    not _real_env_ready(),
    reason="keiba-yosou の DB / lightgbm が無い環境ではスキップ (実 predict_race 依存)",
)


# build_samples が使う list_races(jra_only=True, require_confirmed=True) と同一のフィルタで
# 「最初の JRA 確定レース日」を引く。horse_races だけを見ると地方/海外の確定レース (JRA 外) を
# 拾ってしまい、その日を起点にすると jra_only で全除外され窓が空になる (JRA は年始 1/1-4 は非開催)。
_FIRST_JRA_RACE_DATE_SQL = """
    SELECT MIN(race_year || race_month_day) AS d FROM races
     WHERE (race_year || race_month_day) >= ?
       AND CAST(track_code AS INTEGER) BETWEEN 1 AND 10
       AND EXISTS (
         SELECT 1 FROM horse_races h
          WHERE h.race_year=races.race_year AND h.race_month_day=races.race_month_day
            AND h.track_code=races.track_code AND h.kaiji=races.kaiji
            AND h.nichiji=races.nichiji AND h.race_num=races.race_num
            AND CAST(h.confirmed_order AS INTEGER) = 1
       )
"""


def _first_race_window(days: int = 8, anchor: str = "20240101") -> tuple[str, str] | None:
    """anchor 以降の最初の JRA 確定レース日から days 日ぶんの (from, to) を返す。

    過去走履歴・オッズが揃う近年 (既定 2024 年以降) に寄せることで predict_race が実データで
    意味のあるスコアを返し、テストが速く・安定する。開催週末を確実に含むよう既定 8 日窓。
    anchor 以降に該当が無ければ DB 全体の最古 JRA 確定日にフォールバック。1 件も無ければ None。
    """
    with kb.open_conn() as conn:
        row = conn.execute(_FIRST_JRA_RACE_DATE_SQL, (anchor,)).fetchone()
        if not row or not row["d"]:
            row = conn.execute(_FIRST_JRA_RACE_DATE_SQL, ("00000000",)).fetchone()
    if not row or not row["d"]:
        return None
    start = str(row["d"])
    end = str(int(start) + days)  # 数日程度なので単純加算で十分 (月跨ぎは list_races 側が吸収)
    return start, end


@requires_real_db
def test_build_samples_pick_end_to_end():
    window = _first_race_window(days=3)
    if window is None:
        pytest.skip("DB に確定レースが無い")
    samples = kb.build_samples(window[0], window[1], subject="pick")
    if not samples:
        pytest.skip(f"{window} に pick 対象レースが無い")

    for s in samples:
        assert 0.0 <= s.pred <= 1.0            # raw_blended_probability は確率
        assert s.won in (0, 1)
        assert s.top3 in (0, 1)
        assert s.ret is None or s.ret >= 0.0
        # 主要な軸が埋まっている
        for key in ("track", "surface", "distance", "sire_line", "popularity"):
            assert key in s.axes
        # surface は英字キーに正規化されている (日本語ラベル漏れの回帰防止)
        assert s.axes["surface"] in {"turf", "dirt", "jump", "other"}

    # pick は 1 レース 1 件 → won 合計は的中レース数、pred は縮退していない
    preds = [s.pred for s in samples]
    assert min(preds) > 0.0
    assert max(preds) < 1.0
    # 系統分類が全馬 unknown ではない (horse_masters join が効いている)
    assert any(s.axes["sire_line"] != "unknown" for s in samples)
    # 芝レースの condition が全て unknown に潰れていない
    # (_surface_of の日本語→英字マッピング欠落バグの回帰防止: surface が "芝" のままだと
    #  axes.condition_key の surface=="turf" 分岐が外れ、芝の condition が全部 unknown になる)
    turf = [s for s in samples if s.axes["surface"] == "turf"]
    if turf:
        assert any(s.axes["condition"] != "unknown" for s in turf)


@requires_real_db
def test_build_samples_all_has_more_than_pick():
    window = _first_race_window(days=3)
    if window is None:
        pytest.skip("DB に確定レースが無い")
    pick = kb.build_samples(window[0], window[1], subject="pick")
    alls = kb.build_samples(window[0], window[1], subject="all")
    if not pick or not alls:
        pytest.skip(f"{window} に対象レースが無い")
    # all は全出走馬 → pick (1 レース 1 件) より必ず多い
    assert len(alls) > len(pick)
    # all は 1 レース 1 頭だけ won=1 (勝ち馬)。pick は暫定レースを除くため、
    # all の勝ち馬数 (=レース数) は pick の勝ち馬数以上になる。
    assert sum(s.won for s in alls) >= sum(s.won for s in pick)
