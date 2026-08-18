"""探索の候補特徴量セット定義。build (matrix) と search で同じ定義を共有する。

- full_specs():  全 compute/current 特徴 + 豊富な可変集計バリアント (幅×深さ最大)。
                 compute を含むので行列構築は重い (全期間で数時間) が、時間をかけて網羅する。
- cheap_specs(): compute を使わない軽量版 (可変集計 + 現在属性 + オッズ)。数分で構築。

可変集計は netkeiba の「左側条件(距離/競馬場/芝ダート) × 右側(直近N走)」に対応。
"""

from __future__ import annotations

from . import model

_AGG_METRICS = ("agg_top3_rate", "agg_win_rate", "agg_avg_finish",
                "agg_avg_final3f", "agg_avg_popularity")
_LOOKBACKS = (3, 5, 8, 10, None)
_MATCHES = ([], ["surface"], ["distance"], ["track"], ["surface", "distance"])


def _agg_variants() -> list[dict]:
    out = []
    for met in _AGG_METRICS:
        for lb in _LOOKBACKS:
            for m in _MATCHES:
                out.append({"key": met, "lookback": lb, "match": list(m)})
    return out


def full_specs() -> list[dict]:
    """全 compute/current 特徴 + 全可変集計バリアント。"""
    base = [{"key": k} for k, f in model.FEATURES.items()
            if f.kind in ("compute", "current")]
    return base + _agg_variants()


def cheap_specs() -> list[dict]:
    """compute 非依存の軽量セット (現在属性 + 可変集計)。"""
    base = [{"key": k} for k, f in model.FEATURES.items() if f.kind == "current"]
    return base + _agg_variants()


# ---------------------------------------------------------------------------
# MAIBuilder 設計書 v0.3 §4.2 の基底列定義
#   集計対象 9 項目 × 一致条件 4 種 × 期間 11 種 = 396 基底列
# ---------------------------------------------------------------------------
MAIB_STEP2_METRICS: tuple[str, ...] = (
    "agg_avg_finish",             # 着順
    "agg_margin",                 # 着差 (導出)
    "agg_time_index",             # タイム指数 (導出)
    "agg_prize",                  # 獲得本賞金 (生 SE から復元)
    "agg_corner_first",           # 第1コーナー着順 (生 RA から復元)
    "agg_corner_last",            # 最終コーナー着順
    "agg_gain_first_to_last",     # 第1→最終コーナーの着順上昇
    "agg_gain_first_to_finish",   # 第1コーナーからの着順上昇
    "agg_gain_last_to_finish",    # 最終コーナーからの着順上昇
)
# 一致条件(4): 全レース / 距離が同じ / 競馬場が同じ / 芝ダート別
MAIB_MATCHES: tuple[list[str], ...] = ([], ["distance"], ["track"], ["surface"])
# 期間(11): 全レース + 直近1〜10
MAIB_LOOKBACKS: tuple[int | None, ...] = (None, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)

# STEP1 (単項目特徴量): レジストリから参加者に意味が伝わるものを curate。
# 全公開はしない (UI の認知負荷を優先。設計書 §4.1)。
MAIB_STEP1_KEYS: tuple[str, ...] = (
    "popularity",                     # 人気(市場)
    "burden_weight", "burden_delta",  # 斤量・斤量変化
    "horse_weight_change",            # 馬体重変化
    "days_since_last",                # 出走間隔
    "draw_position",                  # 枠順
    "jockey_win_rate", "jockey_recent_30d_top3_rate", "jockey_track_top3_rate",
    "trainer_win_rate", "trainer_recent_30d_top3_rate",
    "sire_surface_top3_rate", "sire_distance_top3_rate", "sire_going_top3_rate",
    "dam_sire_surface_top3_rate",
    "fit_course", "fit_course_distance", "fit_distance", "fit_going", "fit_surface",
    "horse_track_top3_rate", "horse_recent_90d_top3_rate",
    "recent_avg_finish", "recent_trend_delta", "last_finish",
    "avg_final_3f", "best_final_3f_rank",
    "recent_4corner_avg_position", "recent_4corner_position_change",
)


def maib_step2_specs() -> list[dict]:
    """設計書 §4.2 の STEP2 基底列 (9 × 4 × 11 = 396)。"""
    out = []
    for met in MAIB_STEP2_METRICS:
        for m in MAIB_MATCHES:
            for lb in MAIB_LOOKBACKS:
                out.append({"key": met, "lookback": lb, "match": list(m)})
    return out


def maib_step1_specs() -> list[dict]:
    """設計書 §4.1 の STEP1 単項目特徴量 (curate 済み)。"""
    return [{"key": k} for k in MAIB_STEP1_KEYS if k in model.FEATURES]


def maib_all_specs() -> list[dict]:
    """MAIBuilder が事前計算する全基底列 (STEP1 + STEP2)。"""
    return maib_step1_specs() + maib_step2_specs()


# ---------------------------------------------------------------------------
# 判断A (2026-07-26 確定、設計書 v0.3 §2): 「人気(市場)」を参加者AIから除外する
# ---------------------------------------------------------------------------
# 実測 (docs/evidence/20260726_FINDINGS_preset_weights.md):
#   人気を含めると ◎ の 95.9% が1番人気と一致し、項目を追加しても ◎ が変わるのは
#   0.2〜2.2% だけ。「選んだ項目が根拠になる」という商品定義と両立しない。
#
# **基底列としては残す。** 除外するのは参加者に見える面 (選択肢・スコア・寄与) のみ:
#   - 行列キャッシュ (4.6GB) と列構成指紋の互換性が保たれる
#   - ベースラインの「1番人気AI」は horse の pop を直接使うのでこの列に依存しない
#   - 列別学習は列ごとに独立なので、除外しても他列の重みは変わらない
#     (tests/test_presets.py::test_per_column_fit_is_independent_of_other_columns)
PARTICIPANT_EXCLUDED_KEYS: frozenset[str] = frozenset({"popularity"})

# 2025-07-05〜2026-07-25 の未学習期間3,702レースで全425列を単独再生した監査結果。
# 基底列はキャッシュ互換のため残すが、参加者には選ばせない。
#
# - recent_4corner_*: 学習重み0・検証期間でも使用可能レース0
# - agg_gain_first_to_last: 44セルすべてが一様予想以下
# - days_since_last / draw_position: 検証期間で一様予想を僅かに下回り、重みも極小
#
# 「選ぶと印が動く」だけでなく、未学習期間でも予測方向が再現した項目だけを公開する。
PARTICIPANT_RETIRED_KEYS: frozenset[str] = frozenset({
    "recent_4corner_avg_position",
    "recent_4corner_position_change",
    "agg_gain_first_to_last",
    "days_since_last",
    "draw_position",
})

PARTICIPANT_UNAVAILABLE_KEYS: frozenset[str] = (
    PARTICIPANT_EXCLUDED_KEYS | PARTICIPANT_RETIRED_KEYS
)


def maib_participant_step1_specs() -> list[dict]:
    """参加者が選べる STEP1 (人気除外・未学習期間監査を適用済み)。"""
    return [s for s in maib_step1_specs()
            if s["key"] not in PARTICIPANT_UNAVAILABLE_KEYS]


def maib_participant_step2_metrics() -> tuple[str, ...]:
    """参加者が選べる STEP2 集計対象。基底列定義自体は変更しない。"""
    return tuple(k for k in MAIB_STEP2_METRICS if k not in PARTICIPANT_UNAVAILABLE_KEYS)
