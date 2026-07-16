# ai-builder — 競馬予想「戦略ビルダー」

セグメント別の **的中率 / 回収率 / calibration gap** を、**hold-out 分離 + Wilson CI +
過学習ガード**付きで探索する read-only 診断ツール。netkeiba の予想ビルダー的な
「条件を組んで指標を見る」体験を、`keiba-yosou` の検証規律の上で実現する。

> **重要**: 本ツールは観察・探索専用。見つけた「戦略」を **買い目に自動適用しない**。
> keiba-yosou の P12 事故 (TEST 通年 184% → PRODUCTION 45% 暴落) の教訓から、実採用には
> 必ず hold-out 再現 + 人間判断 + 月次 rolling 監視を挟む。

## keiba-yosou との関係 (重要)

ai-builder は `keiba-yosou` を **兄弟ディレクトリとして read-only で参照**する。

```
dev/
  keiba-yosou/          # 予想本体。ai-builder はここを import して使うだけ (一切変更しない)
    data/keiba.db
    predictor/sire_lines.py, rules.py ...
    scripts/backtest.py, bias_scan.py ...
  ai-builder/           # このリポジトリ
    builder/
    tests/
```

- **import は一方通行**: ai-builder → keiba-yosou のみ。keiba-yosou 側は ai-builder を知らないので、本ツールが予想出力に影響することは構造的にない。
- **DB は read-only** (`open_db_readonly`)。ingest / GUI / 予想生成と競合しない。
- 場所は環境変数で上書き可: `KEIBA_YOSOU_PATH` / `KEIBA_DB_PATH`。

## セットアップ

```bash
# 1. keiba-yosou と並べて clone
cd dev
git clone https://github.com/K-takeuchi0810/keiba-yosou.git      # 既にあれば不要
git clone https://github.com/K-takeuchi0810/ai-builder.git
cd ai-builder

# 2. デモ (keiba-yosou 不要、即動作確認)
python -m builder.server --demo
#   → http://127.0.0.1:8770/  軸=condition で探索すると soft が reproduced=✅ になり、
#     軸=weather_wet では wet が TRAIN のみで非 reproduced になる (過学習ガードのデモ)

# 3. テスト
python -m pytest tests/ -q
```

## 実データで動かす (keiba-yosou 接続)

```bash
# keiba-yosou を ../keiba-yosou に置き、data/keiba.db を用意した状態で
python -m builder.server --from 20240101 --to 20251231 --subject pick
```

> ⚠ **未検証の 1 箇所**: `builder/keiba_bridge.py` の `_race_predictions()` が呼ぶ
> keiba-yosou `predict_race` の引数形式・戻り値フィールドは、本キット作成環境では実 DB を
> 実行できなかったため未検証。**ai-builder セッションの最初のタスクとして検証**すること。
> keiba-yosou の `scripts/bias_scan.py` が「predict → セグメント別 calibration」の実証済み
> 参照実装なので、そこと突き合わせて配線を確定する。それ以外 (explore コア・axes 導出) は
> `tests/` で合成データ単体テスト済み。

## 構成

| ファイル | 役割 | テスト |
|---|---|---|
| `builder/explore.py` | 探索コア (Wilson CI / calibration gap / hold-out / reproduced 判定)。**純粋関数** | ✅ |
| `builder/axes.py` | セグメント軸の導出 (surface/condition/weather/distance/season/系統…)。**純粋関数** | ✅ |
| `builder/keiba_bridge.py` | keiba-yosou 連携 (DB 走査 + predict + 分類 → Sample)。⚠ predict 配線は要検証 | — |
| `builder/demo.py` | 合成データ生成 (keiba-yosou 不要のデモ) | 間接 |
| `builder/server.py` | 最小 read-only 探索サーバ (stdlib のみ) | 手動スモーク |
| `builder/config.py` | パス・既定値 (環境変数で上書き可) | — |

## 設計上のガードレール (netkeiba builder との違い)

1. **hold-out 強制分離**: `explore()` は `split_date` で TRAIN/HOLDOUT を必ず分ける。
2. **reproduced 判定**: TRAIN で有意 (n≥min_n かつ mean_pred が実勝率 Wilson 区間外) かつ
   HOLDOUT でも n≥min_n かつ gap 符号一致 → はじめて `reproduced=True`。過学習セルを弾く。
3. **サンプル数ゲート**: `min_n` 未満のセルは `status=insufficient`。「バイアス」と呼ばない。
4. **read-only**: DB 書き込みなし。config/weights.json/calibrator.json を触らない。
5. **買い目非直結**: 戦略保存はデータ出力のみ。実運用採用は人間判断 + 月次監視とセット。

## 開発ロードマップ (MVP → 拡張)

- [x] 探索コア (pure) + 単体テスト
- [x] 軸導出 (pure) + 単体テスト
- [x] 最小 read-only サーバ + デモデータ
- [ ] keiba_bridge の `predict_race` 配線を実 DB で検証 (bias_scan 参照) ← **最初のタスク**
- [ ] 複数フィルタ (AND) の UI 入力
- [ ] 2 軸クロス集計
- [ ] 戦略の保存/読込 (JSON、観察用ラベル付き)
- [ ] bootstrap による return% の CI

## 由来

keiba-yosou セッション (2026-07-16) で作成したスターターキット。keiba-yosou 側の
`webapp/aggregate.py` (傾向集計)・`scripts/bias_scan.py` (セグメント calibration)・
`predictor/sire_lines.py` (系統分類) の設計を踏襲・再利用している。
