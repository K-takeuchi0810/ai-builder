# 長時間ジョブの運用手順 (実際の障害から得た教訓)

## 起きたこと (2026-07-26)

425列の行列ビルド (6年並列・数時間) の最中に **Claude Code アプリが落ち、6プロセス全滅**した。
バックグラウンドで起動したプロセスがアプリの子プロセスだったため、親と一緒に死んだ。

## 被害と、被害を抑えた仕組み

| 項目 | 結果 |
|---|---|
| 保全された月キャッシュ | **44 / 66 ヶ月 (3.06 GB)** |
| 失った作業 | 各年の処理中だった1ヶ月ぶん (最大6ヶ月 ≈ 各15分) |
| 破損ファイル | **0 件** (原子的書き込み `.tmp` → `replace` が効いた) |
| 2021年 | 12/12ヶ月 完了済みだったため無傷 |

**月次チェックポイント (`builder/matrix.py`) が設計どおり機能した。** これが無ければ数時間を失っていた。

## 手順 (今後の長時間ジョブは必ずこうする)

### 1. アプリから切り離した独立プロセスで起動する

Git Bash の `nohup ... &` は Windows では親から切り離せない。**PowerShell の `Start-Process`** を使う:

```powershell
$repo = 'C:\Users\kizun\dev\ai-builder'
$py   = 'C:\Users\kizun\dev\keiba-yosou\.venv64\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path "$repo\out\logs" | Out-Null
foreach ($y in 2021,2022,2023,2024,2025,2026) {
  $code = "from builder import matrix; from builder.specs import maib_all_specs; matrix.build_matrix('${y}0101','${y}1231', maib_all_specs())"
  $ps = @"
`$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONPATH='$repo'
Set-Location '$repo'
& '$py' -u -c "$code" *> '$repo\out\logs\maib_$y.log'
"@
  Set-Content -Path "$repo\out\logs\run_$y.ps1" -Value $ps -Encoding utf8
  Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',
    '-File',"$repo\out\logs\run_$y.ps1" -WindowStyle Hidden
}
```

### 2. チェックポイントを必ず持たせる

- 途中結果を**逐次**保存する (`matrix.build_matrix` は月単位)。
- 書き込みは**原子的**に (`.tmp` に書いて `replace`)。中断で壊れたファイルを残さない。
- 再実行は**キャッシュ済み単位をスキップして再開**できること。

### 3. 進捗はファイルで確認できるようにする

プロセスが死んでも状態が分かるよう、ログとキャッシュを見れば進捗が復元できる形にする:

```bash
# 425列の月キャッシュ数 (指紋で絞る)
python -c "from pathlib import Path; from builder import matrix; from builder.specs import maib_all_specs; \
h=matrix._col_hash(matrix._columns(maib_all_specs())); \
print(len(list(Path('out/matrix/months').glob(f'm_*_{h}.json'))), '/ 66')"
```

## 10月デモへの適用 (設計書 §12 信頼性要件)

- 当日朝のバッチも同じ方針で**独立プロセス**として起動する。進行中にツールが落ちても止まらない。
- 当日行列 (`matrix_daily`) も原子的書き込みなので、途中で落ちてもキャッシュは壊れない。
- 進行台本には「ビルドの進捗はキャッシュ数で確認する」手順を書いておく。
