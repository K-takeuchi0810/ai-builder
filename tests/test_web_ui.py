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


def test_no_external_resources():
    """外部ホストへの参照ゼロ (Webフォント・CDN・アナリティクス)。

    デモ会場のネットワークに依存させないため、および §0 の「Webフォントを追加しない」。
    """
    for name, text in ALL.items():
        assert "//fonts.googleapis" not in text, name
        assert "@import" not in text, name          # 外部CSS読み込み
        assert "@font-face" not in text, name       # Webフォント
        # http(s) の外部参照 (コメント中の説明も含めて禁止して単純化する)
        assert not re.search(r"https?://(?!127\.0\.0\.1|localhost)", text), name


def test_no_framework_or_build_tooling():
    for name, text in CODE.items():
        low = text.lower()
        for banned in ("react", "vue.js", "tailwind", "bootstrap", "jquery",
                       "import ", "require("):
            assert banned not in low, f"{name} に {banned!r}"


def test_no_font_size_below_the_floor():
    """§0 DON'T: フォントサイズ .72rem 未満を新設しない。

    v0.2 モックアップには .62〜.70rem が残っていたので、機械的に閉じる。
    """
    small = []
    for m in re.finditer(r"font-size:\s*([0-9.]+)rem", CSS):
        v = float(m.group(1))
        if v < 0.72:
            small.append(m.group(0))
    assert small == [], f"下限 .72rem 未満: {small}"
    assert "--fs-min:.72rem" in CSS.replace(" ", "")


def test_no_px_font_size_below_the_floor():
    """px 指定で下限を回り込まないこと (.72rem = 11.52px 相当)。"""
    small = [m.group(0) for m in re.finditer(r"font-size:\s*(\d+)px", CSS)
             if int(m.group(1)) < 12]
    assert small == [], f"12px 未満: {small}"


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
    # sessionStorage は id の保存/読み出しのみ
    for m in re.finditer(r"sessionStorage\.(getItem|setItem|removeItem)\(([^)]*)\)", JS):
        assert "CFG_ID_KEY" in m.group(2), f"id 以外を保存している: {m.group(0)}"
    # 復元は必ず API を叩く
    assert "/api/configs/" in JS


def test_no_roi_or_recovery_rate_in_ui():
    """§10 全体DON'T: 回収率・ROI をUIのどこにも出さない。

    検査はコメントを除いた実コード (規則を説明するコメントは正当)。
    """
    for name, text in CODE.items():
        assert "ROI" not in text, name
        assert "回収率" not in text, name
        assert "払戻" not in text and "払い戻し" not in text, name
    # API の値も参照しない (そもそも predict/backtest は ROI キーを持たない)
    assert "roi" not in CODE["app.js"]
    assert "payout" not in CODE["app.js"] and "tan" not in CODE["app.js"].split("const")[0]


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
    # 一覧行 (.row 内) にスコア文字列を差し込んでいない
    row = re.search(r"<button class=\"row\">(.*?)</button>", JS, re.S)
    assert row is not None
    assert "fmtScore" not in row.group(1)
    assert "scorebar" in row.group(1)


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
