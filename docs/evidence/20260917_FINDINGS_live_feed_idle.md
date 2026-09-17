# 平日に「速報情報を更新中」が出続ける — 原因と修正 (2026-09-17)

サービス再起動後のヘルスチェックで、開催変更フィード(0B14)が
`age_seconds: 315708` (3.7日) / `fresh: false` になっていた。

## 1. 調査 — 故障ではなかった

取り込みタスクの状態:

```
MAIBuilder Live JRA Data Controller   Ready     最終 09/17 06:05 (結果 0)  次回 12:05
MAIBuilder Live JRA Data              Disabled  最終 09/13 18:05 (結果 0)
```

コントローラが1日4回動き、`configure-live-jvdata-task.py` に開催の有無を尋ねて、
**開催が無ければ取り込みタスクを意図的に無効化する**設計だった。

今日 (木) の判定:

```json
{"enabled": false, "target": "20260917", "reason": "no_official_race", "race_count": 0}
```

判定スクリプトを日付を変えて実行し、**今週末は自動で再有効化される**ことを確認した:

| 日付 | 判定 | 稼働時間帯 |
|---|---|---|
| 9/17(木) 9/18(金) | 無効 | 開催なし |
| 9/19(土) 9/20(日) 9/21(月・祝) | **有効** | 07:00〜21:00 |
| 9/22(火) | 無効 | 開催なし |

**フィードは壊れていない。** 最後の開催日 (9/13) 以降に開催が無いだけ。

## 2. 見つけた本当の問題

`live_status.source()` は `age_seconds <= max_age_seconds` だけで `fresh` を決めていた。
`max_age_seconds = 90` は**開催中の速報間隔を前提にした閾値**なので、
開催が無い日は必ず超える。

UI は `live_source_fresh === false` で「速報情報を更新中」のチップと警告カードを出す。
つまり **平日はずっと警告が出たまま**になり、

- 直しようがない警告が常に出ている
- **本当に取り込みが止まったときに区別がつかない**

直せない警告は、直せる警告を埋もれさせる。

## 3. 修正

`source()` に「取り込みが動くはずの時間帯か」(`expected`) を渡し、4状態を返す。

| state | 意味 | fresh |
|---|---|---|
| `fresh` | 期待どおり新しい | `True` |
| `stale` | **動くはずの時間帯なのに古い ← これだけが異常** | `False` |
| `idle` | 取り込みが動かない時間帯 (開催なし等)。鮮度を判定しない | `None` |
| `unknown` | 一度も記録が無い | `False` |

`fresh` を `None` にすることで、UI の `=== false` に当たらなくなる
(UI 側の変更は不要)。`age_seconds` は隠さずそのまま返す。

`expected` を渡さなければ従来どおり bool を返すので、**取得タスク側の
`due()` は挙動を変えない** (呼ばれている時点で取り込みの時間帯にいる)。

`api._feed_expected(date)` が `live_schedule.decide()` で窓を判定する。
1リクエストごとに DB を引かないよう、日付単位で 600 秒キャッシュする
(窓の境界は1日のあいだ動かない)。

## 4. 実測

```
窓内・記録が今   → state=fresh   fresh=True
窓内・記録が古い → state=stale   fresh=False   ← 異常として出る
窓外・記録が古い → state=idle    fresh=None    ← 警告を出さない
記録なし         → state=unknown fresh=False
```

## 5. テスト

`tests/test_live_status.py` に5件、`tests/test_api.py` に4件。
`expected` を無視する実装に戻すと2件落ちることを確認済み。

窓の判定は時刻を注入して検証する (`_feed_expected(date, now=...)`)。
開催日でも**窓の外なら expected=False** であることを、開始前・終了後・別日で固定した。

## 6. 残した仕様

`age_seconds` は idle のときも実際の経過秒を返す。
「古いこと自体」は事実なので隠さない — 判定に使わないだけ。
