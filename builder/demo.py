"""合成 Sample 生成 — keiba-yosou 未接続でも explore/server を即動かすためのデモデータ。

意図的に 2 種のシグナルを埋める:
- condition=soft: TRAIN/HOLDOUT 両方で mean_pred > actual (過信) が再現 → reproduced=True になるはず
- weather=snow:   TRAIN のみノイズで gap、HOLDOUT では消える → reproduced=False になるはず
この 2 つで「過学習ガードが効いていること」を目視できる。
"""

from __future__ import annotations

import random

from .explore import Sample

_TRACKS = ["05", "06", "08", "09"]
_SURFACES = ["turf", "dirt"]
_COND = ["firm", "good", "yielding", "soft"]
_WEATHER_WET = ["dry", "wet"]
_LINES = ["サンデー系", "ノーザンD系", "ミスプロ系", "キングマンボ系", "ロベルト系"]


def make_samples(n: int = 6000, seed: int = 42) -> list[Sample]:
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        # 前半 (2024) を TRAIN、後半 (2025) を HOLDOUT に割る
        year = "2024" if i < n // 2 else "2025"
        mmdd = f"{rng.randint(1,12):02d}{rng.randint(1,28):02d}"
        cond = rng.choice(_COND)
        wet = rng.choice(_WEATHER_WET)
        base = 0.30                                   # モデルの平均主張勝率
        actual_p = base
        # 埋め込み①: soft は実勝率が下がる=過信 (両期間で再現)
        if cond == "soft":
            actual_p = base - 0.12
        # 埋め込み②: snow(wet) は TRAIN だけノイズで実勝率が下がるが HOLDOUT では戻る
        if wet == "wet" and year == "2024":
            actual_p = base - 0.10
        won = 1 if rng.random() < actual_p else 0
        top3 = 1 if (won or rng.random() < 0.35) else 0
        ret = (1.0 / max(base, 0.05)) * 0.8 if won else 0.0   # 大まかな払戻
        out.append(Sample(
            date=f"{year}{mmdd}",
            axes={
                "track": rng.choice(_TRACKS),
                "surface": rng.choice(_SURFACES),
                "condition": cond,
                "weather_wet": wet,
                "sire_line": rng.choice(_LINES),
                "popularity": rng.choice(["1", "2", "3", "4-6", "7-9", "10+"]),
                "month": mmdd[:2].lstrip("0"),
            },
            pred=base + rng.uniform(-0.02, 0.02),
            won=won, top3=top3, ret=ret,
        ))
    return out
