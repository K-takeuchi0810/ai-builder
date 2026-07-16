# 予想ロジック分析官 (計量予測モデラー) 採点

対象: `builder/keiba_bridge.py` の predict 配線 (ai-builder / type-B 計測・探索ツール)
日付: 2026-07-17 ／ レビュア: prediction-logic-analyst (keiba-yosou `.claude/agents` 定義に準拠)

> **実装者 addendum (2026-07-17, レビュー後)**: 本採点の HOLD 根拠である `_surface_of` の
> 日本語→英字マッピング欠落バグを修正済み (`builder/keiba_bridge.py`: `_SURFACE_JP_TO_EN` を
> 追加し bias_scan.surface_key と同一の `{"芝":"turf","ダート":"dirt","障害":"jump"}.get(...,"other")`
> を適用)。回帰防止テストも追加 (`tests/test_bridge.py`: surface が英字集合であること + 芝レースの
> condition が unknown 一色でないこと)。提案 3 (払戻欠損) は `get_payout_row`/`payout_from_row` へ
> 移行し race 単位で 1 回引く形にしたが、payout_present の Sample 保持は explore スキーマ拡張に
> なるため今回は見送り (確定データでは実害小、既知の軽微な下方バイアスとして記録)。

## 判定: HOLD → (surface 修正により FAIL 事由解消。再採点で PASS 相当を見込む)

**理由**: 中核配線 (Q1-Q5: リーク規律・tentative・pick 定義・cache 共有・train-serve) は bias_scan と一致し reconcile 可能。ただし `_surface_of` が Japanese→English マッピングを欠き (`keiba_bridge.py:52-59`)、**surface 軸ラベルが日本語のまま漏れ + condition 軸が芝レースで全て "unknown" に潰れる**。type-B の「誤読を招く出力」ゲートに抵触するため採用保留。P25 固有ゲート (factorial C1-C5 / fresh odds / market_snapshot / calibrator refit / bonus_candidate) は type-B のため **N/A (対象外)**。
**根拠ファイル**: `builder/keiba_bridge.py:52-59,183`, `builder/axes.py:65-69`, `keiba-yosou/scripts/bias_scan.py:113-115,406,419-434`, `keiba-yosou/predictor/rules.py:228-234,1448,1454`
**次アクション**: `_surface_of` に bias_scan と同じ `{"芝":"turf","ダート":"dirt","障害":"jump"}` マッピングを追加 → surface/condition 軸を再生成。global_train ↔ GLOBAL REF の突合は surface に非依存なので現状でも成立見込み。 ✅ 対応済 (addendum 参照)

## 総合: 3.8 / 5 (参考スコア)

## 項目別 (type-B 用に再解釈)

- **測定量の正しさ / リーク規律: 5/5** — `build_samples` は `p.raw_blended_probability` を測る (`keiba_bridge.py:183`)。これは LGBM blend 直後・calibrator 適用前・race 内 Σ=1 の量で calibrator fit 入力そのもの (`rules.py:228-234,1448`)。calibration gap 測定にはこれが正しい。`win_probability` (calibrator+market_blend+odds_discount 後、race 内非正規化、確率解釈不可) を排除。bias_scan も同じ量を使う (`bias_scan.py:425,432`)。
- **母集団定義 / reconciliation 整合: 4/5** — `list_races(jra_only=True, require_confirmed=True)`、no-horses/no-winner スキップ、pick=`preds[0]`、tentative 丸ごとスキップ、untrusted→`ret=None` — すべて bias_scan (`bias_scan.py:395-434`) と一致。`won` は pick 馬の `confirmed_order==1`、bias_scan は `top.horse_num==actual_win.horse_num`。actual_win は confirmed_order==1 の馬なので論理等価。top3 も等価。留保: (a) bias_scan の mean_pred は 4 桁丸め (`bias_scan.py:348`)・explore は無丸め → reconcile は 4 桁で合わせる、(b) `include_tentative` を bridge は False 固定、bias_scan は引数 → 突合時 bias_scan を include_tentative=False で回す、(c) bridge は払戻行欠損=0 扱いで presence を分離しない → 的中かつ payout 行欠損時に ret が僅かに下方バイアス (確定データで実害小)。
- **確率変換 / 正規化と校正の相互作用: 4/5** — 消費側は再校正も再正規化もせず Σ=1 の raw prob に対し `gap = mean_pred - actual_rate` を測る (`explore.py:96-98`)。多段変換の二重取り込みなし。hold-out 分離 + Wilson CI + 符号再現ゲート (`explore.py:157-163`) は過学習防止として妥当。
- **コード正しさ / 設計整合性: 2/5** — **バグ**: `_surface_of` は `track_type()` の戻り (芝/ダート/障害, `web/codes.py:61-73`) をマッピングせず返す。docstring は "turf/dirt/jump/other" と主張するが実際は日本語。結果 `axes.condition_key` (`axes.py:66`) が芝レースで常に dirt_condition 経路に落ち、芝の condition が全て "unknown" に潰れる。`if surface=="jump"` も発火しない。bias_scan は `surface_key` で必ずマッピングする (`bias_scan.py:113-115`)。 ✅ addendum で修正済。
- **train-serve 整合 / 再現性: 4/5** — 計測経路は bias_scan と同一の `predict_race(horses, conn, race, cache)` で serve 側の別経路が無く skew 構造なし。feature_cache 全レース共有は安全: cache key が `("past", blood, before, 12)` で `before` に日付を含む (`features.py:994-1000`)。留保: 探索結果を artifact 化する段では keiba-yosou の RULES_VERSION + git_sha 記録が必須 (現状 list 返却のみで N/A、前方要件)。

## 停止条件チェック

- [x] P25 固有ゲート (factorial/fresh odds/market_snapshot/calibrator refit/bonus_candidate): **N/A (type-B)**
- [x] リーク規律 (raw vs calibrated prob): raw_blended_probability を正しく選択
- [x] 母集団 paired 整合 (bias_scan と同一): list_races/winner gate/tentative/pick すべて一致
- [x] 期間明示: from_date/to_date を引数で受ける
- [ ] 誤読を招く出力なし: **抵触** — surface 日本語漏れ + condition 芝 unknown 潰れ ✅ 修正済
- [x] 再現性メタ: 現状 artifact 未出力 → N/A。artifact 化時に keiba-yosou git_sha + RULES_VERSION 必須 (前方要件)
- [x] payout 欠損 race の扱い: `get_payout` は欠損=0 (presence 分離は未移植、留保 c)

## 反証の試み

- 「pick の global_train が bias_scan の GLOBAL REF と一致する」→ 不一致シナリオ (pick 定義/tentative 母集団ズレ) を検証 → **不成立 (=一致する)**。両者 pick=`preds[0]`、同一 list_races、同一 winner gate、同一 is_tentative スキップ、同一 round(...,6) raw prob。よって n・mean_pred・actual_rate・gap は (4 桁丸め・include_tentative=False を合わせれば) 一致するはず。smoke の n=240/won=0.1917/pred_mean=0.1753 (gap≈-0.016) も ◎ 本命として妥当。
- 「feature_cache 全レース共有は安全」→ 反例「同一馬が別日に別 past_runs を持つのに古い cache を返す」→ **不成立 (=安全)**。key に `before` (日付) が入る。
- (docstring)「_surface_of は turf/dirt/jump/other を返す」→ **成立せず (=バグ)**。track_type は芝/ダート/障害を返す。これが唯一潰せなかった反証で HOLD の根拠。 ✅ 修正済。

## 主な改善提案 (優先順)

1. **`_surface_of` にマッピング追加** — bias_scan と同一の `{"芝":"turf","ダート":"dirt","障害":"jump"}.get(jp,"other")`。surface/condition 軸が正常化。 ✅ 対応済。
2. **surface/condition 軸の値をテストで固定** — surface が英字集合であること + 芝レースで condition が unknown 一色でないことを assert。 ✅ 対応済。
3. **払戻欠損の下方バイアスを可視化** — `get_payout` を `get_payout_with_presence` に替え Sample に payout_present を持たせる。→ race 単位 `get_payout_row`/`payout_from_row` へ移行済。payout_present 保持は explore スキーマ拡張のため見送り (実害小)。

## 前回からの差分

初回採点 (ai-builder bridge の過去 scorecard なし)。
