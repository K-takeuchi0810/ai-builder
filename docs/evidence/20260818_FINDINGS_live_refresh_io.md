# 常駐プロセスが4日間で 2.1 TB 読んでいた — 原因と修正 (2026-08-18)

`python -m builder.api --date 20260809 --port 8781 --no-backtest` が
**4日間で累計 2,136 GB のディスク読み取り**を行い、Windows Defender の
リアルタイム検査を巻き込んで CPU を恒常的に消費していた (ファンが回りっぱなし)。

**この修正を消さないでください。** 消すと同じ症状に戻ります。理由は §3 と §4。

---

## 1. 実測

調査時点で稼働中だったプロセス (`Win32_Process` カウンタ):

| 項目 | 値 |
|---|---|
| 累計ディスク読み取り | **2,136.5 GB** |
| 累計 CPU 時間 | 13,008 秒 (3.6 時間) |
| 書き込み | 0 GB |
| メモリ | 77 MB (リークなし) |

約30秒周期で **145 MB** のバーストが出ていた。
`_LIVE_REFRESH_INTERVAL_SECONDS = 25.0` + 1周の処理時間と一致する。

## 2. 経路

```
api.py  _live_refresh_loop()      5秒ごとに起床
  → api.py  _refresh_current_daily()   25秒経過していれば実処理
    → matrix_daily.refresh_live(daily)  当日の全レース (36) をループ
      → matrix_daily.py: cached["payouts"] = payout_data.for_race(cached)
        → payouts.py  for_race()   ← ここ
```

`for_race()` は **着順が確定しているレースほど毎周回必ず通る**
(`if any(h.get("order") == 1 ...)`)。

## 3. 原因1: 読む対象が広すぎた

JV-Data は**レコード種別ごとにファイルが分かれている**。20260809 で実測:

```
HR*20260809*.jvd  →  1件 /  0.02 MB   ← HRレコードはこのファイルだけ
*20260809*.jvd    → 19件 /  9.20 MB   ← H1/H6(票数) O1〜O6(オッズ) RA SE を巻き込む
```

`glob(f"*{date}*.jvd")` が票数・オッズ・出走表まで候補にしていた。
それらのファイルに HR レコードは**構造的に入っていない**。

mtime 降順に読むので、`HRSW` に到達するまでの累計は:

```
SESW 268.8KB → RASW 44.8KB → O6SW 2928.0KB → O5SW 432.2KB → O4SW 141.8KB
→ O3SW 93.3KB → O2SW 71.8KB → O1SW 33.9KB → HRSW 25.3KB  = 3.95 MB
3.95 MB × 36レース = 142 MB / サイクル      (報告された実測 145 MB と一致)
142 MB × (4日 ÷ 25秒) ≒ 2,000 GB          (累計実測 2,136 GB と一致)
```

**修正**: `HR*{date}*.jvd` と `0B12_{date}{track}{race_num}_*.jvd` だけを候補にする。

## 4. 原因2: 同じファイルを毎周回・毎レース読み直していた

確定した払戻は基本的に変わらないのに、25秒ごとに全レース分を読み直していた。
さらにレースごとに `data/raw/RACE` (2,912ファイル) を glob していたため、
**ディレクトリ走査だけで 36レースあたり 340 ms** かかっていた。

**修正**: 2段のキャッシュを入れた。

| 何を | 鍵 | 効果 |
|---|---|---|
| ファイル内容の索引 (レースキー → 払戻) | `(パス, mtime_ns, サイズ)` | 内容が同じなら `stat()` だけ |
| ディレクトリ一覧 | 開催日 (TTL 5秒) | glob を1周1回に |

### なぜ「確定済みならスキップ」にしなかったか

降着・審議で**払戻が訂正される**ことがあり、0B12 の速報も後から届く。
`cached["payouts"]` があればスキップする実装だと、**訂正を永久に取り逃す**。
実資金の精算に使う数字なので採らなかった。

**ファイルの署名 (mtime_ns + サイズ) を鍵にすれば、訂正されたときだけ読み直す。**
この性質は `tests/test_payouts.py::test_a_corrected_file_is_picked_up` で固定した。

## 5. 原因3: 確定後もライブ更新が同じ間隔で回り続けた

`follow_today = args.date is None` なので `--date` 指定時は日付が固定される。
**全レース確定後にライブ更新を止める/緩める条件が無かった。**

**修正**: `api._refresh_interval(daily)` で状況に応じて変える。

| 状況 | 間隔 |
|---|---|
| 固定日 · 全レース確定 かつ 払戻取得済 | **600 秒** |
| 固定日 · 未発走が残っている | 25 秒 |
| 固定日 · 払戻がまだ取れていない | 25 秒 |
| 当日追従 (`--date` なし) | 25 秒 (日付が変わりうるので短いまま) |

止めずに伸ばすだけにしたのは §4 と同じ理由 (払戻訂正)。

## 6. 修正後の実測 (20260726 · 36レース)

| | 修正前 | 修正後 |
|---|---|---|
| 1サイクルの読み取り | **142 MB** | **0 MB** (初回のみ 0.025 MB) |
| 1サイクルの所要 | 352 ms | 0.4 ms (一覧更新時のみ 14 ms) |
| 払戻の取得 | 36/36 | **36/36** (変わらず) |
| 4日間の累計 (推定) | 約 2,000 GB | **約 0 GB** |

## 7. 変更した箇所

| ファイル | 内容 |
|---|---|
| `builder/payouts.py` | `_listing` `_hr_candidates` `_index_file` `_indexed` を追加、`for_race` を索引利用に。`parse_hr` / `settle` / `_digits` / `_combo` は変更なし |
| `builder/api.py` | `_IDLE_REFRESH_INTERVAL_SECONDS` `_all_settled` `_refresh_interval` を追加、`_refresh_current_daily` の間隔判定を状況依存に |
| `tests/test_payouts.py` | **新設** (14件)。`payouts.py` にテストが1件も無かった |
| `tests/test_api.py` | 更新間隔のテスト3件を追加 |

### テストで固定した性質

- HR 全8券種の解釈と、順序あり券種 (馬単・三連単) の向き
- **HR 以外のファイルを読まないこと** (`test_only_hr_files_are_read`)
- **2周目でファイルを読み直さないこと** (`test_repeated_calls_do_not_read_again`)
- **訂正されたファイルを拾うこと** (`test_a_corrected_file_is_picked_up`)
- キャッシュが日をまたいでも無限に増えないこと
- 精算で**順序あり券種を並べ替えて的中にしない**こと

## 8. 対応していないもの

### keiba-yosou 側 (`scripts/backtest.py:543`)

`list_races()` の

```sql
WHERE (race_year || race_month_day) BETWEEN ? AND ?
```

は文字列連結のため `idx_races_date` が効かず全表スキャンになる
(`EXPLAIN QUERY PLAN` → `SCAN races`)。以下で同一結果・インデックス利用になる。

```sql
WHERE race_year=? AND race_month_day BETWEEN ? AND ?
```

**「keiba-yosou 側のファイルは一切変更しない」制約があるため、ここでは直していない。**
62,804行 · 約8MB/サイクルなので今回の主因ではない。

### 常駐プロセスの停止手順

文書化されていなかった。当面は以下。

```
powershell -Command "Get-Process python* | Where-Object { $_.Path -like '*venv64*' } | Stop-Process"
```

`--date` を指定した検証用プロセスを起動したまま放置しないこと。
§5 の修正で読み取りは消えたが、開催日ごとに落とす運用が前提。

---

## 9. あわせて直したフレークテスト

`test_config_names_are_unique_and_archiving_is_reversible` が稀に落ちていた。

```
AssertionError: At index 0 diff: 'マイAI 2' != 'マイAI'
```

**コードは仕様どおりで、テストの期待が時刻に依存していた。**

`list_configs` の並びは `(archived, -updated_at, name)` で、`updated_at` は
`int(time.time())` の**秒精度**。テストは同じ名前で2件保存して
`["マイAI", "マイAI 2"]` を期待していたが、

| 2回の save | 並び |
|---|---|
| 同じ秒に入る | `["マイAI", "マイAI 2"]` (名前で並ぶ) |
| 秒境界をまたぐ | `["マイAI 2", "マイAI"]` (新しいものが先) |

docstring は「新しいものが先」なので後者が正しい挙動。時刻を差し替えて
**両方を決定的に再現**して確認した。

**修正**:

- 名前の一意化のテストは並び順に依存させない (`a["name"]` / `b["name"]` と集合で確認)
- 並び順は `test_config_list_puts_the_newest_first` で**時刻を固定して**別に検証
  (1分差で新しいものが先、同じ秒なら名前順)

`test_api.py` + `test_payouts.py` + `test_betslip.py` を10回連続実行して失敗0件。

### 残る仕様上の制約 (直していない)

同じ秒に作った2件は作成順ではなく名前順になる。`updated_at` が秒精度のため。
実害は一覧の並びだけなので、保存形式を変える価値は無いと判断した。
