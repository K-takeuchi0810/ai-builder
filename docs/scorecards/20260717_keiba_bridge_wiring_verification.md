# keiba_bridge 配線検証サマリ (2026-07-17)

`builder/keiba_bridge.py` を keiba-yosou の実 API に合わせて修正し、実 DB で配線を検証した記録。
bridge は keiba-yosou を read-only 参照するのみで、keiba-yosou 側は 1 行も変更していない。

## 修正内容 (実 API への追従)

| 項目 | 修正前 (キット想定) | 修正後 (実 API) |
|---|---|---|
| predict 呼び出し | `predict_race(conn, date, track, ...)` | `predict_race(horses, conn=conn, race=race, cache=feature_cache)` |
| 確率フィールド | dict/dataclass 両対応の曖昧アダプタ | `Prediction.raw_blended_probability` (属性アクセス) |
| is_tentative | per-prediction 誤用 | per-race 1 回 (`is_tentative(preds)`)、pick は暫定レース丸ごとスキップ |
| pick 定義 | `mark == "◎"` フィルタ | `preds[0]` (bias_scan と同一 → reconcile 可能) |
| 払戻 | `get_payout(conn, race)`→dict 想定 | `get_payout_row`/`payout_from_row(..., "tan")/100.0` (円 int)、race 単位で 1 回引く |
| odds ゲート | `bt._race_odds_untrusted(conn, race)` | `race_odds_untrusted(horses, race, popularity_config()["max_snapshot_age_min"])` |
| 系統名 | `h.get("sire_name")` (常に None) | `blood_register_num` で `horse_masters` を read-only join |
| surface | `track_type()` の日本語をそのまま | `{"芝":"turf","ダート":"dirt","障害":"jump"}.get(...,"other")` (bias_scan と同一) |

キット指示書の誤り 2 点を訂正: `_popularity_config` → 実名 `popularity_config`、
`bt._race_odds_untrusted` → 実名 `race_odds_untrusted`。

## スモーク (実 DB, 20250101-20250131, subject=pick)

- n=240、pred min/mean/max = 0.0581 / 0.1753 / 0.3915 (確率として妥当、0<pred<1)
- won_rate=0.1917、top3_rate=0.4542 (◎ 本命として妥当)
- ret 全 240 件算出 (歴史的確定オッズ → odds_untrusted=0)、mean_ret=0.6038
- sire_line 分布は 8 系統に分散 (unknown 潰れなし → horse_masters join 有効)

## bias_scan 突合 (DoD #2) — 完全一致

同一期間・subject=pick で `scripts/bias_scan.py` の GLOBAL REF と builder を比較:

| 指標 | builder build_samples | bias_scan GLOBAL REF |
|---|---|---|
| n | 240 | 240 (races=240) |
| mean_pred | 0.1753 | 0.175 |
| actual_rate | 0.1917 | 0.192 |
| calibration_gap | -0.0164 | -0.016 |
| 暫定スキップ | 0 | skip_tentative=0 |
| odds untrusted | 0 (ret_n=240) | odds_untrusted=0 |

母集団・測定量・pick 定義が bias_scan と一致していることを実測で確認。

## surface/condition 軸バグの発見と修正 (prediction-logic-analyst 指摘)

`_surface_of` が `web.codes.track_type()` の日本語ラベルをマッピングせず返していたため、
`axes.condition_key` の `surface=="turf"` 分岐が芝レースで永久に外れ、芝の condition が
全て "unknown" に潰れていた (jump も発火せず)。マッピング追加後の 3 日窓検証:

- surface 分布: dirt=27, turf=20, jump=1 (英字キーに正規化、jump も出現)
- turf condition 分布: firm=17, good=2, yielding=1 (unknown 潰れ解消)

回帰防止テストを `tests/test_bridge.py` に追加。

## expert-review (DoD #5)

- **data-pipeline-engineer: PASS (4.3/5)** — read-only 規律・SQL 正当性・接続ライフサイクルすべて充足、停止条件抵触なし。
- **prediction-logic-analyst: HOLD → 修正で解消** — リーク規律・母集団整合・train-serve は一致。surface バグを HOLD 根拠として指摘 → 修正済。

両 scorecard は同ディレクトリに保存。改修タイプは両者とも **type-B** (診断/読み取り専用計測、予測ロジック不変) と分類され、P25 固有ゲートは N/A。

## 既知の軽微な留保 (実害小・今後の改善候補)

- 払戻行欠損 (`payout_row` が None) と「外れ」を区別しない (どちらも ret=0)。確定データでは実害小。
- 探索結果を artifact 化する際は keiba-yosou の git_sha + RULES_VERSION を記録すること (provenance)。
