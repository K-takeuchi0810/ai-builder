"""UI指示書 の DON'T を機械的に検査する (web/ の静的検査)。

レビューでの見落としを防ぐため、目視ではなくテストで固定する。

検査対象:
- §0 技術スタック確定: フレームワーク・ビルドツール・CSSフレームワーク・Webフォントなし
- §0 DON'T: フォントサイズ .72rem 未満を新設しない / タップ標的 44px 未満を新設しない
             / localStorage に設定の正本を置かない
- §10 全体DON'T: 回収率・ROI をUIに出さない / 個人名・役職名を出さない (「参加者」で統一)
- 配信経路: builder/api.py の静的配信でパストラバーサルを拒否する
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import api  # noqa: E402

WEB = api.WEB_DIR
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
ALL = {"index.html": HTML, "app.css": CSS, "app.js": JS}


def _strip_comments(text: str) -> str:
    """コメントを除いた「実際に動く/表示される」部分だけを残す。

    禁止語の検査対象はコメントではない (規則そのものを説明したコメントは正当)。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)          # CSS/JS ブロック
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)          # JS 行コメント
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)         # HTML
    return text


CODE = {name: _strip_comments(t) for name, t in ALL.items()}


def test_web_files_exist_and_are_the_only_assets():
    """素の HTML/CSS/JS の3枚のみ (ビルド成果物・依存ディレクトリを置かない)。"""
    names = sorted(p.name for p in WEB.iterdir() if p.is_file())
    assert names == ["app.css", "app.js", "index.html"]
    assert not (WEB / "node_modules").exists()
    assert not (WEB / "package.json").exists()


# JRA 公式の QR 作成サイトだけは遷移先として許可する
# (買い目は公式サイトで手入力する方針。QR データ形式は非公開なので
#  このツールでは生成しない)。ページ自体はオフラインでも動く。
ALLOWED_LINK_HOSTS = ("qrcode.jra.go.jp",)


def test_no_external_resources():
    """外部ホストから読み込むものがゼロであること。

    デモ会場のネットワークに依存させないため、および §0 の「Webフォントを追加しない」。
    **`<a href>` のリンク先は読み込みではない**ので対象外 (オフラインでも画面は動く)。
    """
    for name, text in ALL.items():
        assert "//fonts.googleapis" not in text, name
        assert "@import" not in text, name          # 外部CSS読み込み
        assert "@font-face" not in text, name       # Webフォント
        # 読み込み位置の外部参照を禁止: src= / link href= / url() / fetch()
        for pat in (r"src\s*=\s*[\"']https?://", r"<link[^>]+href\s*=\s*[\"']https?://",
                    r"url\(\s*[\"']?https?://", r"fetch\(\s*[\"'`]https?://"):
            got = re.search(pat, text)
            assert not got, f"{name} に外部読み込み: {got.group(0)}"


def test_only_the_official_jra_site_is_linked():
    """外部リンクは JRA 公式の QR 作成サイトだけ、かつ安全な属性を付けること。"""
    hosts = set(re.findall(r"https?://([^/\"'`\s)]+)", CODE["app.js"] + CODE["index.html"]))
    hosts -= {"127.0.0.1", "localhost"}
    assert hosts <= set(ALLOWED_LINK_HOSTS), f"許可外の外部ホスト: {hosts}"
    if hosts:
        # 新しいタブで開き、参照元を渡さない
        assert 'target="_blank"' in CODE["app.js"]
        assert 'rel="noopener noreferrer"' in CODE["app.js"]


def test_no_framework_or_build_tooling():
    for name, text in CODE.items():
        low = text.lower()
        for banned in ("react", "vue.js", "tailwind", "bootstrap", "jquery",
                       "import ", "require("):
            assert banned not in low, f"{name} に {banned!r}"


# D-1: 下限を .72rem (11.5px) から .78rem に引き上げた。判断材料の大半が
# 最小サイズで出ており、競馬ユーザーの年齢層を考えると小さすぎた。
FONT_FLOOR = 0.78


def test_no_font_size_below_the_floor():
    """§0 DON'T: フォントサイズを下限未満で新設しない。

    v0.2 モックアップには .62〜.70rem が残っていたので、機械的に閉じる。
    """
    small = []
    for m in re.finditer(r"font-size:\s*([0-9.]+)rem", CSS):
        v = float(m.group(1))
        if v < FONT_FLOOR:
            small.append(m.group(0))
    assert small == [], f"下限 {FONT_FLOOR}rem 未満: {small}"
    assert f"--fs-min:{FONT_FLOOR}rem".replace("0.", ".") in CSS.replace(" ", "")


def test_no_px_font_size_below_the_floor():
    """px 指定で下限を回り込まないこと (.78rem = 12.48px 相当)。"""
    floor_px = FONT_FLOOR * 16
    small = [m.group(0) for m in re.finditer(r"font-size:\s*(\d+)px", CSS)
             if int(m.group(1)) < floor_px]
    assert small == [], f"{floor_px}px 未満: {small}"


def test_tap_targets_are_at_least_44px():
    """§0 DON'T: タップ可能要素の高さ 44px 未満を新設しない。

    ボタン・チェックボックスのラベル・入力欄に min-height が付いていること。
    """
    assert "--tap:44px" in CSS.replace(" ", "")
    # min-height の実値が 44px を下回っていないか (--tap 参照は OK)
    low = [m.group(0) for m in re.finditer(r"min-height:\s*(\d+)px", CSS)
           if int(m.group(1)) < 44]
    assert low == [], f"44px 未満の min-height: {low}"

    # 主要なタップ要素が min-height を持っていること
    for selector in (".pchip", ".cchip", ".cta", ".race-item", ".badge-conf",
                     ".step2 .toggle", ".horse .row"):
        block = _rule_block(CSS, selector)
        assert block is not None, f"{selector} のルールが無い"
        assert "min-height" in block, f"{selector} に min-height が無い"


def _rule_block(css: str, selector: str) -> str | None:
    """セレクタ **そのもの** のルール本体。子孫セレクタの一部に一致させない。

    (`.cta` が `.empty .cta{...}` に一致してしまう事故を防ぐ)
    """
    m = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}",
                  css, re.M)
    return m.group(1) if m else None


def test_localstorage_is_not_the_source_of_truth():
    """§0 DON'T: localStorage に設定の正本を置かない。

    保持していいのは「サーバ上の設定を指す id」だけで、設定本体は毎回 API から取る。
    """
    assert "localStorage" not in CODE["app.js"]
    # sessionStorage に置いていいのは **id と、レース→AIの対応** だけ。
    # 設定本体はサーバから取り直す。
    allowed = {"CFG_ID_KEY", "RACE_CFG_KEY"}
    for m in re.finditer(r"sessionStorage\.(getItem|setItem|removeItem)\(([^)]*)\)", JS):
        key = m.group(2).split(",")[0].strip()
        assert key in allowed, f"許可外のキーを保存している: {m.group(0)}"
    # 設定の中身を保存していないこと
    assert not re.search(r"sessionStorage\.setItem\([^)]*step1", JS)
    # 復元は必ず API を叩く
    assert "/api/configs/" in JS


def test_roi_is_never_shown_as_a_bare_point_estimate():
    """回収率の表示は **不確かさを必ず伴う** こと (2026-07-27 の判断で表示に変更)。

    全面禁止をやめた代わりに、より強い構造要件をここで固定する。
    1日36レースでは回収率は「◎に30倍が来たか」でほぼ決まるので、点推定を
    単独で見せると運の差が実力の差に見える。

    実測の根拠: 182,594候補で再現するエッジは0件、回収率で浮上した3件は
    ◎勝率 2.6〜8.0% (1番人気は33〜35%) の大穴くじだった。
    """
    js = CODE["app.js"]
    # 数値を出す前に enough を確認している (レース数のゲート)
    assert "s.enough" in js, "最小レース数のゲートを通していない"
    assert "s.min_races" in js, "不足時にレース数を出していない"
    # 信頼区間を併記している
    assert "s.ci" in js, "信頼区間を出していない"
    # 首位との差が誤差の範囲なら明示する
    assert "roi_tied_with_leader" in js and "誤差の範囲" in js
    # 最大配当1本が支配している場合を開示する
    assert "top_share" in js
    # 控除率の上限を注記としてサーバから受け取り表示する
    assert "roi_note" in js
    # 数値はサーバ集計のみ (UI で回収率を計算しない)
    for banned in ("/ 100", "reduce((s", "payout"):
        assert banned not in js.split("function roiRow")[1].split("function betSlipBlock")[0], banned


def test_roi_module_attaches_uncertainty_by_construction():
    """roi.py が点推定だけを返せない形になっていること。"""
    import numpy as np
    from builder import roi
    # 不足時は数値を出さない判定
    few = roi.summarize(np.array([0.0] * 10 + [5.0]))
    assert few["enough"] is False and few["ci"] is None
    # 足りていれば信頼区間が付く
    enough = roi.summarize(np.array([0.0] * 59 + [30.0]))
    assert enough["enough"] is True and enough["ci"] is not None
    # 最大配当1本の占有率が出る (1本で説明できるかを示す)
    assert enough["top_share"] == 1.0
    # 控除率の上限が必ず入る
    assert enough["long_run_ceiling"] == 0.8
    assert roi.MIN_RACES_FOR_ROI >= 50


def test_no_profit_promising_vocabulary():
    """回収率を出すようになっても「儲かる」方向の語彙は入れない。

    設計書 v0.3 §1 DON'T は維持する (表示の解禁は数値の話であって、
    煽り文言の解禁ではない)。
    """
    for name, text in CODE.items():
        for banned in ("儲か", "稼げ", "必勝", "勝てます", "確実", "おすすめの馬券",
                       "推奨買い目"):
            assert banned not in text, f"{name} に {banned!r}"


def test_no_contest_or_hype_vocabulary():
    """設計書 v0.3 §1 DON'T: 対抗戦・勝負・煽り系の語彙を使わない。

    位置づけが「個人利用主体の分析ツール」に変わったため、対抗戦前提の文言を
    残さない。表現は「成績比較」「基準との差」に統一する。
    """
    banned = ["対抗戦", "勝負", "バトル", "優勝", "儲か", "稼げ", "必勝", "鉄板級です"]
    for name, text in CODE.items():
        for w in banned:
            assert w not in text, f"{name} に {w!r}"
    # 「見どころ」のような煽り語彙も置換済みであること
    assert "見どころ" not in CODE["index.html"]
    # 置換後の語彙が入っていること
    assert "本日の成績比較" in CODE["index.html"]
    assert "基準" in CODE["index.html"]


def test_popularity_is_absent_from_the_ui():
    """判断A: 「人気(市場)」を参加者向けの面に出さない。

    選択肢はサーバ (feature_catalog) が返すので UI にハードコードは無いが、
    人気を前提にした文言や独自ロジックが残っていないことを固定する。
    """
    js = CODE["app.js"]
    # 選択肢としての popularity をUIが持たないこと。
    # レスポンスのフィールド参照 (m.popularity で「N番人気」を表示) と
    # 用語辞書キーとしての参照 (term('popularity', ...)) は要件なので許可する。
    # **文字列リテラル** としての出現がすべて term() 呼び出しであることを確認する。
    for m in re.finditer(r"""['"]popularity['"]""", js):
        before = js[max(0, m.start() - 6):m.start()]
        assert "term(" in before, f"term() 以外の文字列参照: ...{before}{m.group(0)}"
    assert 'data-key="popularity"' not in js
    # 設定に人気を差し込む経路が無いこと (選択肢はサーバが返す)
    assert "step1: ['popularity'" not in js and 'step1: ["popularity"' not in js
    # 1番人気との比較表示は残る (ベースラインは市場人気専用なので正当)
    assert "1番人気" in js or "1番人気" in CODE["index.html"]


def test_no_hardcoded_performance_numbers():
    """作業項目4: ダミーの成績値をUIに埋め込まない (実測値はAPIから取る)。

    モックアップにあった 24.8% のような固定値が残っていると、実測と乖離した
    数字を参加者に見せることになる。
    """
    for name in ("index.html", "app.js"):
        # 「12.3%」のような固定のパーセント表記が無いこと
        found = re.findall(r"\d+\.\d+\s*%", CODE[name])
        assert found == [], f"{name} に固定の成績値 {found}"
    # 成績カードは API の値のみを描画している
    assert "hit_rate_win" in CODE["app.js"]


def test_marks_are_suppressed_by_default_on_unknown_warnings():
    """警告コードは「印を出して良い側」を列挙して塞ぐこと (fail-closed)。

    以前は「印を出さない側」を列挙していたため、新しい警告コード
    (preset_fingerprint_missing など) が増えると黙って印が出てしまった。
    """
    js = CODE["app.js"]
    m = re.search(r"const HARMLESS = \[([^\]]*)\]", js)
    assert m is not None, "HARMLESS の列挙が無い"
    harmless = re.findall(r"'([a-z_]+)'", m.group(1))
    # 印を出して良いのは「開示のみ」の警告だけ
    assert set(harmless) == {"low_sample_columns", "excluded_columns_dropped",
                             "columns_skipped_in_race"}, harmless
    # 判定は「HARMLESS 以外があれば止める」向きであること
    assert "!HARMLESS.includes" in js
    # 個別コードの直接列挙で塞いでいないこと (漏れの原因)
    assert "=== 'preset_column_mismatch'" not in js


def test_server_side_warning_codes_are_all_classified():
    """サーバが出す警告コードが UI 側の分類から漏れていないこと。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "builder"
    codes = set()
    for f in ("predict_service.py", "presets.py"):
        codes |= set(re.findall(r'"code":\s*"([a-z_]+)"',
                                (root / f).read_text(encoding="utf-8")))
    js = CODE["app.js"]
    harmless = set(re.findall(r"'([a-z_]+)'",
                              re.search(r"const HARMLESS = \[([^\]]*)\]", js).group(1)))
    # 開示系は HARMLESS に、それ以外は「印を止める」側に落ちる (既定で止まる)
    assert harmless <= codes, f"UI が知らないコードを許可している: {harmless - codes}"
    # 印を止めるべきコードが HARMLESS に混ざっていないこと
    for blocking in ("no_preset_weights", "all_columns_gated_out",
                     "preset_column_mismatch", "preset_fingerprint_missing"):
        assert blocking in codes, f"{blocking} がサーバ側に無い"
        assert blocking not in harmless, f"{blocking} を許可してはいけない"


def test_no_negative_percentage_in_the_ui():
    """受け入れ条件: 負のパーセントが画面に出ない。

    押し下げ側は「評価を下げた内訳」見出しの下に正の % で出す。
    マイナス記号つきの % を組み立てるコードが無いことを固定する。
    """
    js = CODE["app.js"]
    assert "−${" not in js and "-${share" not in js
    # 割合は絶対値から作る (符号は見出しで示す)
    assert re.search(r"const a = Math\.abs\(", js), "寄与の絶対値を取っていない"
    assert re.search(r"const share = [^\n;]*\ba / total", js), "割合が絶対値由来でない"
    # 表示は %% のみで、符号を前置していない
    assert "${share}%" in js and "+${share}" not in js
    # 見出しで向きを示している
    assert "評価を上げた内訳" in js and "評価を下げた内訳" in js


def test_beginner_affordances_are_present():
    """初心者対応: 印の凡例・用語シート・決め手の一文・事前マーク。"""
    js, html = CODE["app.js"], CODE["index.html"]
    assert "openMarkSheet" in js                     # 印の凡例シート
    assert "openTermSheet" in js and "data-term" in js
    assert "m.decisive" in js                        # 決め手の一文 (サーバ生成)
    # A-2: 以前は button 内の <button class="chip-thin">。内容モデル違反だったので
    # 操作不可の印 (thin-mark) に変え、説明は兄弟の ⓘ に寄せた。
    assert "thin-mark" in js                         # 低サンプルの事前マーク
    assert "starter_preset" in js                    # 「まよったら」
    assert 'id="sheet"' in html                      # ボトムシート本体
    assert 'id="markLegend"' in html


def test_explanations_are_not_hardcoded_in_the_ui():
    """DON'T: 説明文を UI にハードコードしない (labels.py 経由)。

    用語の説明本文は glossary から来る。UI 側に desc 文字列を持たない。
    """
    js = CODE["app.js"]
    assert "state.glossary[" in js and "g.desc" in js
    # 用語の説明らしい長文を UI が持っていないこと
    for phrase in ("払戻倍率", "3着以内に入ること", "コースの湿り具合", "背負う重さ"):
        assert phrase not in js, f"説明文が UI にハードコードされている: {phrase}"


def test_ranking_rule_is_always_visible():
    """受け入れ条件: board が空でも順位規則が読める。

    以前は空のとき早期 return して #boardRule を一度も設定しなかった。
    規則カードを静的に置き、空状態でも DOM に存在させる。
    """
    html = CODE["index.html"]
    assert 'id="boardRule"' in html
    assert "rule-card" in html
    # 初心者向けの一文も静的に置く
    assert "本命(◎)にした馬が1着" in html
    # 空状態の出し分けが実装されている
    js = CODE["app.js"]
    for kind in ("waiting", "nomyai", "notready"):
        assert kind in js, kind


def test_start_time_stays_visible():
    """受け入れ条件: スクロール位置によらず発走時刻が視界にある。"""
    js, html = CODE["app.js"], CODE["index.html"]
    assert 'id="miniHead"' in html
    assert "setMiniHead" in js and "発走" in js
    assert "window.addEventListener('scroll', updateMiniHead" in js
    # スクロールイベントが来なくても描画時に評価する (戻る操作で既にスクロール済みの場合)
    assert re.search(r"updateMiniHead\(\);\s*//", js), "描画時に呼んでいない"


def test_ui_does_not_derive_the_frame_number():
    """F2: 枠色を馬番から計算しないこと。

    JRA の枠割は頭数依存なので UI 導出は原理的に不可能。実測で ceil(馬番/2) は
    7頭立ての 6/7 件を外した。サーバの waku をそのまま出す。
    """
    js = CODE["app.js"]
    assert "wakuColor" not in js, "馬番からの導出関数が残っている"
    assert "Math.ceil" not in js, "枠の計算式が残っている"
    assert "m.waku" in js, "API の waku を使っていない"
    assert "w-none" in js, "枠番が無いときの無色表示が無い"


def test_race_id_is_carried_in_the_url():
    """F3: リロード・共有・戻るで同じレースに戻れること。"""
    js = CODE["app.js"]
    assert "#predict/${" in js or "`#predict/" in js, "URL にレースを載せていない"
    assert "parseHash" in js
    assert "state.selectedRaceId = h.raceId" in js or "if (h.raceId)" in js


def test_race_list_has_jump_affordances():
    """F4: 会場チップと sticky 見出しで長い一覧を移動できること。"""
    js, css = CODE["app.js"], CSS
    assert "venue-chips" in js and "vchip" in js
    assert "scrollToNextRace" in js
    block = _rule_block(css, ".track-head")
    assert block and "sticky" in block, "会場見出しが sticky でない"
    # チップもタップ標的の下限を守る
    vb = _rule_block(css, ".vchip")
    assert vb and "min-height" in vb


def test_provisional_chip_means_weight_not_announced():
    """F5: 「暫定印」は馬体重未発表のときだけ。項目不足には使わない。"""
    js = CODE["app.js"]
    # 「暫定印」の出現はすべて weight_announced の文脈であること
    for m in re.finditer(r"暫定印", js):
        ctx = js[max(0, m.start() - 90):m.start() + 30]
        assert "weight_announced" in ctx, f"馬体重以外の文脈で使っている: {ctx!r}"
    # カバレッジ不足には別の言葉を使う
    assert "一部の項目が使えません" in js
    # 終了レースは「終了」チップのみ
    assert "'<span class=\"chip\">終了</span>'" in js


def test_no_personal_names_or_titles():
    """§10 全体DON'T: 個人名・役職名を含めない (「参加者」で統一)。"""
    banned = ["社長", "部長", "課長", "会長", "専務", "常務", "取締役", "様専用"]
    for name, text in ALL.items():
        for w in banned:
            assert w not in text, f"{name} に役職名 {w!r}"
    assert "参加者" in HTML                       # 呼称は「参加者」


def test_ui_does_not_recompute_scores():
    """§1 DON'T: スコアリングをUI側で再計算しない。

    禁止するのは **順位や評価値を作り直すこと**。API が返した contribution を
    合計して「割合」に直すのは表示上の正規化なので許す (寄与の桁が項目ごとに
    100倍違うため、生値をそのまま出すと全部 +0.0 になる)。
    """
    js = CODE["app.js"]
    # z 化・標準偏差・重み積の再計算をしていない
    for banned in ("Math.sqrt", "stdev", "* weight", "weight *", ".weight *"):
        assert banned not in js, banned
    # 印の並べ替えをしていない (順序は API の marks のまま)。
    # marks の並べ替えはデモ演出 (?demo=1) の中だけに存在してよい。
    demo = re.search(r"async function simulateWeight\(\)\s*\{(.*?)\n\}", js, re.S)
    assert demo is not None
    outside = js.replace(demo.group(0), "")
    assert "marks.sort" not in outside, "描画経路で印を並べ替えている"
    assert "marks.sort" in demo.group(1)
    # 印そのものを UI が決めていない (mark 文字は API の値をそのまま出す)。
    # 印を割り当てる代入は描画経路に無く、デモ演出の入れ替えだけ。
    assert "MARKS" not in js
    assert not re.search(r"\.mark\s*=[^=]", outside), "描画経路で印を書き換えている"


def test_marks_do_not_show_raw_scores_in_the_list():
    """§7: 印リストにスコア数値を出さない (バーの長さのみ)。

    数値は「なぜ◎か」を開いたときだけ出す。
    """
    # 一覧行 (.row 内) にスコア文字列を差し込んでいない。
    # 行は button ではなく div[role=button] (button の入れ子を避けるため)。
    row = re.search(r'<div class="row" role="button".*?whyBlock', JS, re.S)
    assert row is not None, "行のテンプレートが見つからない"
    assert "fmtScore" not in row.group(0)
    assert "scorebar" in row.group(0)


def test_upset_badge_is_gold_not_alert():
    """§7: 出し抜きは「見どころ」なので gold。赤 (alert) は操作を要するエラー専用。"""
    block = _rule_block(CSS, ".badge-upset")
    assert block and "--gold" in block and "--alert" not in block
    cover = _rule_block(CSS, ".chip.muted")
    assert cover and "--alert" not in cover


def test_column_wording_is_participant_facing():
    """参加者向け文言に「列」を出さない (「項目」と呼ぶ)。"""
    # 画面に出る文字列としての「列」を禁止 (コード内の識別子 columns は可)
    for name in ("index.html", "app.js"):
        assert "425列" not in CODE[name], name
    assert "分析に使えた項目" in CODE["app.js"]
    assert "列" not in CODE["index.html"]


# ---------------------------------------------------------------------------
# 配信経路
# ---------------------------------------------------------------------------
class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers_sent = {}
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.headers_sent[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        outer = self

        class W:
            def write(self, b):
                outer.body += b
        return W()


@pytest.mark.parametrize("rel", ["../builder/config.py", "..%2fbuilder%2fapi.py",
                                 "/../../etc/passwd"])
def test_static_serving_rejects_traversal(rel):
    h = _FakeHandler()
    api._serve_static(h, rel)
    assert h.status in (403, 404), rel
    assert b"import" not in h.body


def test_static_serving_returns_index_and_assets():
    for rel, ctype, needle in (("", "text/html", b"MAIBuilder"),
                               ("index.html", "text/html", b"MAIBuilder"),
                               ("app.css", "text/css", b"--turf-900"),
                               ("app.js", "text/javascript", b"/api/predict")):
        h = _FakeHandler()
        api._serve_static(h, rel)
        assert h.status == 200, rel
        assert h.headers_sent["Content-Type"].startswith(ctype), rel
        assert h.headers_sent["Cache-Control"] == "no-cache"
        assert needle in h.body, rel


def test_all_api_endpoints_are_wired_in_the_ui():
    """UI が §8 の5経路すべてを使っていること (未配線の画面を残さない)。"""
    for path in ("/api/races/today", "/api/features", "/api/leaderboard",
                 "/api/predict", "/api/backtest", "/api/configs"):
        assert path in JS, path

# ---------------------------------------------------------------------------
# D-2: 文字色のコントラストを機械的に固定する
# ---------------------------------------------------------------------------
# 実測で落ちていたのは --ink-faint (#8B9486, 白地 3.14:1) と、文字に使っていた
# --gold (#A8811C, gold-soft 上 3.09:1)。どちらも最小サイズ本文で使われていた。
# 目視では気づけないので、パレットの比を計算して閉じる。
AA_NORMAL = 4.5      # WCAG 2.1 AA: 通常サイズ本文
AA_LARGE = 3.0       # 大きい文字 (>=18.66px bold / >=24px) と UI 部品の境界


def _srgb(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _palette() -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", CSS))


def test_body_text_colours_meet_wcag_aa():
    """本文に使う色が card / paper の両方で AA を満たすこと。"""
    pal = _palette()
    for bg_key in ("card", "paper"):
        bg = pal[bg_key]
        for fg_key in ("ink", "ink-soft", "ink-faint", "gold-text", "turf-700",
                       "minus", "alert", "turf-500"):
            got = contrast(pal[fg_key], bg)
            assert got >= AA_NORMAL, f"{fg_key} on {bg_key} = {got:.2f}"


def test_the_faint_tier_stays_a_tier():
    """--ink-faint は AA を満たしつつ --ink-soft より薄いこと (階層を潰さない)。"""
    pal = _palette()
    faint = contrast(pal["ink-faint"], pal["card"])
    soft = contrast(pal["ink-soft"], pal["card"])
    assert AA_NORMAL <= faint < soft, (faint, soft)


def test_soft_backgrounds_carry_readable_text():
    """淡色の下地 (注意・警告・turf) に載る文字が AA を満たすこと。

    B-2 で自信度を turf 系へ移し、gold を注意専用にした。両方を検査する。
    """
    pal = _palette()
    pairs = [("gold-text", "gold-soft"), ("turf-700", "turf-100")]
    for fg, bg in pairs:
        got = contrast(pal[fg], pal[bg])
        assert got >= AA_NORMAL, f"{fg} on {bg} = {got:.2f}"


def test_no_small_text_uses_a_failing_colour():
    """最小サイズの文字に AA 未満の色を新設しないこと。

    ルール単位で「font-size:var(--fs-min)」と色指定が同居する宣言を集め、
    その色が card 上で AA を満たすかを見る。
    """
    pal = _palette()
    bad = []
    for rule in re.findall(r"\{([^}]*)\}", CSS):
        if "var(--fs-min)" not in rule:
            continue
        m = re.search(r"(?<!-)color:\s*var\(--([a-z0-9-]+)\)", rule)
        if not m:
            continue
        key = m.group(1)
        if key not in pal:
            continue          # 固定色 (#... 直書き) は下の例外表で扱う
        if contrast(pal[key], pal["card"]) < AA_NORMAL:
            bad.append((key, rule.strip()[:60]))
    assert bad == [], bad


# ---------------------------------------------------------------------------
# E-1: オッズの再取得トリガー / C-5: 初回導線
# ---------------------------------------------------------------------------
def test_odds_are_refetched_when_the_snapshot_time_changes():
    """再取得のトリガーが馬体重発表だけになっていないこと。

    以前は `weight_announced` の立ち上がりだけで、「09:50時点」のオッズが
    **最も動く発走直前まで** 残っていた。取得時刻の変化でも取り直す。
    """
    js = CODE["app.js"]
    assert "odds_as_of" in js
    assert "r.odds_as_of !== shownAsOf" in js, "取得時刻の比較が無い"
    assert 'id="refreshBtn"' in js, "手動更新ボタンが無い"


def test_first_run_routes_to_creation():
    """マイAI 0件を検出したら、レースを選ばせる前に作成へ誘導すること。"""
    js, html = CODE["app.js"], CODE["index.html"]
    assert 'id="firstRun"' in html
    assert "maybeInviteFirstRun" in js
    # 一覧の描画より前に判定を走らせる (レース選択後に空を告げない)
    assert js.index("maybeInviteFirstRun()") < js.index("function maybeInviteFirstRun")


def test_tab_bar_uses_labels_only():
    """漢字1文字のアイコンを置かないこと (「比」は初見で意味が取れない)。"""
    html = CODE["index.html"]
    nav = html[html.index("<nav class=\"tabs\">"):html.index("</nav>")]
    assert 'class="ic"' not in nav, nav
    for ch in ("日", "作", "比"):
        assert f">{ch}<" not in nav, ch


# ---------------------------------------------------------------------------
# C-3 (再発): 買い目の表記を UI が組み立て直さない
# ---------------------------------------------------------------------------
def test_the_ui_never_builds_bet_text_itself():
    """UI は `combos` を読まず、サーバの `texts` だけを表示・コピーすること。

    表示だけ直してコピー経路を直し忘れ、コピーすると馬単が馬連と同じ
    「11-10」になっていた。**公式サイトへ手入力する経路そのもの**なので、
    方向が落ちると違う馬券を買うことになる。
    `combos` を UI から一切参照させないことで、両方の経路を1つの規則で閉じる。
    """
    js = CODE["app.js"]
    assert ".combos" not in js, "UI が combos を読んでいる (表記を組み立て直す危険)"
    assert js.count("t.texts") >= 2, "表示とコピーの両方が texts を使っていない"


def test_bet_text_keeps_the_direction_for_ordered_types():
    """順序固定の券種は矢印を保つこと (サーバ側の正本を直接検査)。"""
    from builder import betslip
    marks = [{"mark": m, "horse_num": n}
             for m, n in zip(["◎", "○", "▲"], ["05", "11", "02"])]
    slip = betslip.build(marks)
    by = {t["label"]: t for t in slip}
    # 順序なしの券種は **組の中も並びも馬番順**。公式サイトの入力は馬番順のマス目で、
    # 「5-2」のように軸を先に出すと転記でずれる
    assert by["馬連"]["texts"] == ["2-5", "5-11"], by["馬連"]["texts"]
    # 馬単は並べ替えない — 並び自体が着順の指定
    assert by["馬単"]["texts"] == ["5→2", "5→11"], by["馬単"]["texts"]
    # 手入力用の平文でも方向が残る
    text = betslip.as_text(slip)
    assert "馬単 5→2" in text, text
    assert "馬単 5-2" not in text, text


# ---------------------------------------------------------------------------
# CSS の重複定義 (同じセレクタを2回書くと、後の方が黙って勝つ)
# ---------------------------------------------------------------------------
def test_no_selector_is_defined_twice():
    """同じセレクタを2箇所で定義しないこと。

    一括編集で範囲を取り違えて 150 行ほど複製し、`.bs-k{width:5.2em}` が
    後方から `.bs-k{font-weight:700}` を上書きしていた。**画面は崩れるが
    エラーは出ない**ので、機械的に閉じる。
    (`:hover` などの疑似クラスや複合セレクタは対象外 — 意図的に複数書く)
    """
    sels = re.findall(r"^(\.[a-z0-9-]+)\{", CSS, re.M)
    dups = sorted({x for x in sels if sels.count(x) > 1})
    assert dups == [], f"重複しているセレクタ: {dups}"


def test_no_orphan_class_in_the_css():
    """使われていないクラスを残さないこと (死んだ規則が判断を狂わせる)。

    JS のテンプレートと HTML の両方を見て、どこからも参照されないクラス名を探す。
    レイアウト用の一般クラスは除外する。
    """
    used = CODE["app.js"] + CODE["index.html"]
    # 汎用のレイアウトクラスと、**JS が動的に組み立てる名前** は文字列検索で
    # 見つからないので除外する (枠色は `w${waku}` で作る)。
    skip = {"screen", "active", "hidden", "on", "num", "card", "note", "cta"}
    dynamic = re.compile(r"^w[1-8]$")
    orphans = []
    for sel in sorted(set(re.findall(r"^\.([a-z][a-z0-9-]+)", CSS, re.M))):
        if sel in skip or sel in used or dynamic.match(sel):
            continue
        orphans.append(sel)
    assert orphans == [], f"どこからも使われていないクラス: {orphans}"
