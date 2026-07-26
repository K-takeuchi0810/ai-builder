"""応答性検査: 「選んでも印が動かない」列を機械的に検出する。

## なぜ必要か

396 列は同じ集計対象の条件・期間違いなので相関が極めて高い。全列同時の条件付きロジットでは
重みが相関列間に分散し、個々の係数が潰れやすい。すると参加者が
「賞金 × 同距離 × 直近3走」だけを選んだとき、その列の係数がほぼゼロで **印が人気順と同じ**
になる — エンゲージメント商品として最悪の体験 (選んだ意味が無い)。

列別ゲート通過数 (presets.column_sample_report) はデータ量の検査であって、
**係数が潰れているかは検出できない**。本モジュールはそれを補う:

    各セルを **単独選択** したときの ◎ が、人気1番人気とどれだけ違うか を実測する。

判定:
- `differs_rate`  … ◎ が1番人気と異なるレースの割合
- `flat_rate`     … その列が「使われなかった/寄与ゼロ」で順位が人気と完全一致した割合
- `responsive`    … differs_rate >= min_differs_rate (既定 5%)

## 使い方 (本番学習の直後に必ず走らせる)

    report = check_all_cells(matrix, preset, specs.maib_all_specs())
    dead = [r for r in report["cells"] if not r["responsive"]]

`dead` が多い場合は共線性が原因なので、L2 強度の調整か、**項目ごとの小分け学習**
(9 項目それぞれ独立に fit) への切替を検討する。後者は方式B の精神 (事前学習済み係数) を
保ったまま共線性を回避できる。
"""

from __future__ import annotations

from . import matrix as mx
from . import model


def _favorite(race_prep: dict) -> str | None:
    return race_prep.get("fav")


def cell_response(prep_races: list[dict], columns: list[dict], col_id: str,
                  weight: float) -> dict:
    """1 列を単独選択したときの ◎ が 1番人気とどれだけ違うかを測る。"""
    col = next((c for c in columns if c["id"] == col_id), None)
    if col is None:
        return {"column": col_id, "error": "unknown_column"}
    n = differs = flat = used = 0
    for r in prep_races:
        zs = {num: zc.get(col_id) for num, zc in r["z"].items()}
        have = [v for v in zs.values() if v is not None]
        fav = _favorite(r)
        if fav is None or len(r["z"]) < 2:
            continue
        n += 1
        if not have:
            flat += 1                      # 列が使えず順位が付かない
            continue
        used += 1
        scores = {num: (weight * (zs[num] or 0.0)) for num in r["z"]}
        top = max(scores.items(), key=lambda kv: kv[1])[0]
        if top != fav:
            differs += 1
    return {"column": col_id, "label": col.get("label", col_id), "weight": weight,
            "races": n, "races_column_used": used,
            "differs_from_favorite": differs,
            "differs_rate": round(differs / n, 4) if n else None,
            "flat_rate": round(flat / n, 4) if n else None}


def check_all_cells(matrix: dict, preset: dict, *,
                    min_differs_rate: float = 0.05,
                    date_from: str | None = None, date_to: str = "99999999",
                    limit_races: int | None = 400) -> dict:
    """選択可能な全セルについて応答性を検査する。

    列定義は **行列自身の `columns`** を使う (specs から再導出すると
    prepare_races が使う列と食い違う余地が生まれるため)。
    limit_races で評価レース数を制限できる (全列 × 全レースは重いので既定 400 レース)。
    """
    weights = preset.get("weights") or {}
    columns = matrix.get("columns") or []
    prep = mx.prepare_races(matrix)
    if date_from:
        prep = [r for r in prep if date_from <= r["date"] <= date_to]
    if limit_races:
        prep = prep[:limit_races]

    # 評価レースが無いと「全列が応答しない」と誤報告してしまう (dead_rate=1.0)。
    # 検査不能であることを明示し、判定に使えないと分かる形で返す。
    if not prep:
        return {"n_races_evaluated": 0, "min_differs_rate": min_differs_rate,
                "n_cells": len(columns), "n_responsive": None, "n_dead": None,
                "dead_rate": None, "dead_reasons": {},
                "error": "no_races_in_window",
                "message": (f"{date_from or '(指定なし)'}〜{date_to} に評価対象レースが"
                            "ありません。応答性は判定できません"),
                "cells": []}

    cells = []
    for c in columns:
        w = float(weights.get(c["id"], 0.0))
        if w == 0.0:
            cells.append({"column": c["id"], "label": c.get("label", c["id"]),
                          "weight": 0.0, "races": len(prep), "races_column_used": 0,
                          "differs_from_favorite": 0, "differs_rate": 0.0,
                          "flat_rate": None, "responsive": False,
                          "reason": "zero_weight"})
            continue
        r = cell_response(prep, columns, c["id"], w)
        r["responsive"] = bool((r.get("differs_rate") or 0.0) >= min_differs_rate)
        if not r["responsive"]:
            r["reason"] = ("column_never_usable" if r.get("races_column_used", 0) == 0
                           else "coefficient_collapsed")
        cells.append(r)

    dead = [c for c in cells if not c["responsive"]]
    return {
        "n_races_evaluated": len(prep),
        "min_differs_rate": min_differs_rate,
        "n_cells": len(cells),
        "n_responsive": len(cells) - len(dead),
        "n_dead": len(dead),
        "dead_rate": round(len(dead) / len(cells), 4) if cells else None,
        "dead_reasons": {k: sum(1 for c in dead if c.get("reason") == k)
                         for k in ("zero_weight", "column_never_usable",
                                   "coefficient_collapsed")},
        "cells": cells,
    }
