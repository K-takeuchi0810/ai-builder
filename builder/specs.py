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
