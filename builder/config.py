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
# 賞金も keiba.db にカラムが無いため生 SE レコードから復元する。
PRIZE_INDEX_PATH = Path(os.environ.get(
    "BUILDER_PRIZE_INDEX", _HERE / "out" / "cache" / "prize_index.json"))

# --- プリセット重み (方式B) と表示期間の分離 -------------------------------
# 参加者に見せる的中率が「重みの学習に使ったデータ上の成績」になると僅かに盛られる。
# エッジ商品ではないが「誠実な指標」を売りにする設計なので、学習期間と表示期間を分ける。
PRESET_TRAIN_FROM = os.environ.get("BUILDER_PRESET_TRAIN_FROM", "20210101")
PRESET_TRAIN_TO = os.environ.get("BUILDER_PRESET_TRAIN_TO", "20250630")
# バックテスト再生 (§7) の既定表示期間 = 学習に使っていない期間
DISPLAY_BACKTEST_FROM = os.environ.get("BUILDER_DISPLAY_FROM", "20250701")

# 封印期間の開始日。**項目を選び直しているあいだは見せない**期間。
#
# 表示期間 (20250701〜) はプリセット重みの学習には使っていないので、重みから見れば
# out-of-sample。しかし **参加者の項目選び** から見ればそうではない。良い数字が
# 出るまで選び直せば、その数字は選び直した回数のぶんだけ楽観側に寄る。
# 182,594 候補を機械で探索して out-of-sample のエッジが出なかったのと同じことが、
# 手作業でも起きる (docs/evidence/20260726_FINDINGS_preset_weights.md)。
#
# 既定 20260401 は、表示期間 3,702 レースを 調整側 2,586 / 封印側 1,116 に分ける
# (実測)。両側とも MIN_RACES_FOR_RATE=100 を大きく超える。
DISPLAY_HOLDOUT_FROM = os.environ.get("BUILDER_HOLDOUT_FROM", "20260401")

# 列ごとの「ゲート通過レース数」がこれ未満なら警告 (係数が少数レースに過適合)
MIN_RACES_PER_COLUMN = int(os.environ.get("BUILDER_MIN_RACES_PER_COLUMN", "500"))

PRESET_WEIGHTS_PATH = Path(os.environ.get(
    "BUILDER_PRESET_WEIGHTS", _HERE / "out" / "cache" / "preset_weights.json"))
