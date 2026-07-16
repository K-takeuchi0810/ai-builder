"""ai-builder: 競馬予想「戦略ビルダー」。

セグメント別の的中率/回収率/calibration を hold-out 分離 + Wilson CI + 過学習ガード
付きで探索する read-only 診断ツール。keiba-yosou の予想ロジック・系統分類・レース
データを兄弟ディレクトリとして再利用する (keiba-yosou 側は一切変更しない)。
"""

__version__ = "0.1.0"
