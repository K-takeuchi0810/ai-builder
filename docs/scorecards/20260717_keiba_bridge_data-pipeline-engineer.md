# データパイプライン技術者 (データ基盤 / SRE) 採点

対象: `builder/keiba_bridge.py` の predict 配線 (ai-builder / type-B 診断・読み取り専用計測)
日付: 2026-07-17 ／ レビュア: data-pipeline-engineer (keiba-yosou `.claude/agents` 定義に準拠)

## 判定: PASS

**理由**: 改修タイプ **type-B** (診断/読み取り専用計測ツール、取得・ingest 不変) と分類。type-B 汎用ゲート — read-only 規律・SQL 正当性・スキーマ列実在・大規模 DB 挙動・接続ライフサイクル — をすべて充足し、停止条件抵触なし。P25 固有ゲート (fresh odds/coverage/market_snapshot) は type-B carve-out により **N/A (対象外)**。唯一の実質的留保は「計測 provenance (keiba-yosou git_sha / RULES_VERSION / DB snapshot 版) がどこにも記録されない」点で、これは FAIL でなく改善事項。
**根拠ファイル**: `builder/keiba_bridge.py:45-49,75-91,130-186`、`keiba-yosou/db.py:152-186`、`keiba-yosou/data/schema.sql:152-177`
**次アクション**: build_samples の戻り (または呼び出し側 explore/server の永続化層) に keiba-yosou の `RULES_VERSION` と `git rev-parse HEAD`、DB パス+mtime を付随させ、探索結果が「どの予想版・どの DB 断面」由来か再現可能にする。

## 総合: 4.3 / 5 (参考スコア)

## 項目別

- **read-only 規律 / DB 副作用なさ: 5/5** — DB アクセスは `open_conn()` のみ (`keiba_bridge.py:45-49`) で、これは keiba-yosou の `open_db_readonly` (`db.py:152-186`: URI `mode=ro` + `PRAGMA query_only=ON`、finally で close) をそのまま返す。バッファ全体に DDL/DML/INSERT/UPDATE/`query_only=OFF` は皆無 — 打つ SQL は `_sires` の 1 本の `SELECT` (`:83-87`) と、テストヘルパの 2 本の `SELECT MIN(...)` (`test_bridge.py`) のみ。独立確認: `git -C keiba-yosou status --short` は空 (clean) を再現。

- **SQL 正当性 / スキーマ列実在: 5/5** — `_sires` は完全パラメータ化 (`WHERE blood_register_num=?`) で注入余地なし。選択 4 列はすべて `schema.sql` の `horse_masters` (`:153` PK, `:159-162`) に実在。`sqlite3.Row` 名前アクセスは `open_db_readonly` の `row_factory=sqlite3.Row` (`db.py:180`) と整合。空/None は早期 return、行欠損は全 None → unknown に縮退。呼び出す keiba-yosou 関数の引数もすべて実シグネチャと一致。

- **大規模 DB (~19 GB) 挙動 / クエリ効率: 4/5** — `_sires` は PK 点引きで O(log n)、19 GB でも定数時間。predict_race (240 sample / 722s) が支配的で _sires は無視可能。留保: (a) `get_payout` を per-horse ループ内で呼ぶ (`:165`) ため subject=='all' で同一 payout 行を出走馬数ぶん再取得する N+1 (点引きゆえ実害小)。(b) `list_races` の連結式 BETWEEN は index 不可の全スキャンだが keiba-yosou 既存コードでブリッジ責任外 (参考所見)。

- **接続ライフサイクル / リソース解放: 5/5** — `with open_conn() as conn:` (`:130`) で 1 接続を全レース共有し contextmanager finally (`db.py:184-186`) が close を保証。リークなし。read-only WAL リーダで migration も走らず安価。

- **テスト品質 / 再現性メタ: 3.5/5** — CI-safe 層 (`_sires`) は hit/miss/None/空文字を網羅、in-memory `horse_masters` が選択列を忠実再現。実 DB 依存テストは `_real_env_ready` (DB `.exists()` + lightgbm find_spec) で正しく skip gating。減点理由: builder/ 全体で git_sha/rule_version が 0 ヒット — 生成 Sample がどの予想版・DB 断面由来か未記録。in-memory 返却層で artifact 書き出し層でないため NOT_EVALUABLE には落とさないが provenance 欠如は改善事項。

## 停止条件チェック

- [x] read-only 規律: write / DDL / DML / `query_only=OFF` なし、keiba-yosou untouched (clean 再現)
- [x] SQL 正当性: パラメータ化・列実在・Row アクセス整合
- [x] スキーマ列実在 / フォールバック: 4 列実在、行欠損→unknown 縮退
- [x] 接続ライフサイクル: contextmanager で確実に close
- [ ] 再現性メタ (git_sha/rule_version): **不成立** — 未記録 (artifact 層でないため改善事項に降格)
- N/A P25 fresh-odds / coverage / market_snapshot / bonus_candidate: type-B carve-out で対象外
- N/A payout 欠損 race カウンタ: backtest JSON を出さないため対象外

## 反証の試み

- 「DB は read-only」: フォールバック経路 (`db.py:172-182` の `mode=rw`+`query_only=ON`) でもブリッジは `query_only=OFF` を発行せず SELECT のみ → read-only 保証成立。
- 「schema に列がある」を旧 DB で反証: `open_db_readonly` は migration を走らせない (`db.py:159-161`) ため、`sire_breeding_num` 等を欠く古い horse_masters では `_sires` の SELECT が `no such column` を送出し、`_sires` は例外を捕捉しないため build_samples がクラッシュする経路が理論上存在 (`_classify`/`_surface_of` は try/except で守るが `_sires` は非対称)。現行 DB には全列実在で実害シナリオ不成立だが、堅牢性の軽微な改善余地。

## 主な改善提案

1. **計測 provenance の付随** — build_samples の戻り (または explore/server 永続化点) に `RULES_VERSION`・keiba-yosou HEAD sha・DB パス+mtime を添える。
2. **payout 行の race 単位巻き上げ** — `keiba_bridge.py:165` を per-horse から per-race へ (`get_payout_row` を subject_preds ループ外で 1 回、`payout_from_row` を馬ごと適用)。subject=='all' の N+1 削減。
3. **`_sires` の列欠損フォールバック** — SELECT を try/except (`sqlite3.OperationalError`) で包み列不在時に全 None 縮退させ `_classify`/`_surface_of` と対称化。

## 前回からの差分

前回なし (新規追加ファイル)。初回採点。
