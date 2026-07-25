"""ai-builder の設定。keiba-yosou の場所と DB パスは環境変数で上書き可能。

既定は「ai-builder と keiba-yosou が同じ親ディレクトリに並ぶ」前提:
    dev/
      keiba-yosou/       # 予想本体 (read-only 参照)
        data/keiba.db
      ai-builder/        # このリポジトリ
"""

from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent           # ai-builder/
_DEFAULT_KEIBA = _HERE.parent / "keiba-yosou"             # ../keiba-yosou

KEIBA_YOSOU_PATH = Path(os.environ.get("KEIBA_YOSOU_PATH", _DEFAULT_KEIBA))
KEIBA_DB_PATH = Path(os.environ.get("KEIBA_DB_PATH", KEIBA_YOSOU_PATH / "data" / "keiba.db"))

# 探索の既定サンプル数ゲート (これ未満のセルは「バイアス」と呼ばない)
DEFAULT_MIN_N = int(os.environ.get("BUILDER_MIN_N", "50"))

# hold-out 既定分割日 (この日以降を HOLDOUT)。環境や運用に合わせて調整。
DEFAULT_SPLIT_DATE = os.environ.get("BUILDER_SPLIT_DATE", "20250101")

# 生 JV-Data (read-only 参照) と、そこから復元したコーナー通過順位索引の保存先。
# keiba.db の corner_order_* は全ゼロなので、RA レコードから ai-builder 側で復元する。
KEIBA_RAW_RACE_DIR = Path(os.environ.get(
    "KEIBA_RAW_RACE_DIR", KEIBA_YOSOU_PATH / "data" / "raw" / "RACE"))
CORNER_INDEX_PATH = Path(os.environ.get(
    "BUILDER_CORNER_INDEX", _HERE / "out" / "cache" / "corner_index.json"))
