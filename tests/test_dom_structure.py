"""描画結果の **DOM 構造** を本物の HTML パーサで検査する。

## なぜ文字列検査では足りないか

`<button class="row">` の中に `<button class="term">` を置いた実装が入り、
HTML が button の入れ子を許さないためパーサが内側 button 以降を行の外へ
吐き出した。結果:

- `.why` が `.horse` の子でなくなり `.horse.open .why` が永遠に一致せず、
  タップしても根拠カードが開かない
- `.sub` が空になり、人気・オッズが行の外へ脱落する

テンプレート文字列を grep しても、`.why` が「書かれている」ことしか分からない。
ブラウザと同じパーサに通して **入れ子** を見ないと検出できない。

## 方法 (外部ライブラリなし)

`tests/dom_render.js` が Node 標準の vm と最小 DOM スタブで実際に
`web/app.js` の `renderPredict()` を走らせ、生成 HTML を出す。
それを Python 標準の `html.parser` で解析して構造を検証する。

## このテストの限界 (正直な記載)

`html.parser` は **ブラウザのエラー回復 (要素の吐き出し) を再現しない**。
バグを意図的に戻して確認したところ:

- `test_no_button_is_nested_inside_another_button` … 落ちる (原因を検出)
- `test_row_is_keyboard_operable_without_being_a_button` … 落ちる
- `.why` の親子検査 … **通ってしまう** (パーサが書いたままの入れ子を保つため)

したがって再発防止の主軸は **「button を入れ子にしない」という構造規則** の方。
親子関係そのものはブラウザでの実測で確認する (実施済み)。
jsdom を入れれば回復挙動まで再現できるが、外部依存ゼロの制約があるため採らない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from builder import api, predict_service as svc   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node が無い環境ではスキップ")


class Tree(HTMLParser):
    """開始/終了タグから素朴な木を作る。ブラウザと同じ入れ子解釈を得るため、
    **暗黙の閉じタグは行わない** (不正な入れ子をそのまま検出したい)。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "cls": [], "children": [], "text": "", "parent": None}
        self.cur = self.root
        self.max_button_depth = 0
        self._btn = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        node = {"tag": tag, "cls": (a.get("class") or "").split(),
                "children": [], "text": "", "parent": self.cur, "attrs": a}
        self.cur["children"].append(node)
        if tag in VOID:
            return
        if tag == "button":
            self._btn += 1
            self.max_button_depth = max(self.max_button_depth, self._btn)
        self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node["tag"] != tag:
            node = node["parent"]
        if node is self.root:
            return
        if tag == "button":
            self._btn -= 1
        self.cur = node["parent"]

    def handle_data(self, data):
        self.cur["text"] += data

    # -- helpers -------------------------------------------------------
    def find(self, cls, node=None):
        node = self.root if node is None else node
        out = []
        for ch in node["children"]:
            if cls in ch["cls"]:
                out.append(ch)
            out.extend(self.find(cls, ch))
        return out

    def all_text(self, node) -> str:
        s = node["text"]
        for ch in node["children"]:
            s += self.all_text(ch)
        return s

    def find_tag(self, tag, node=None):
        node = self.root if node is None else node
        out = []
        for ch in node["children"]:
            if ch["tag"] == tag:
                out.append(ch)
            out.extend(self.find_tag(tag, ch))
        return out

    def ancestors(self, node):
        out = []
        p = node["parent"]
        while p is not None:
            out.append(p)
            p = p["parent"]
        return out


def _render(predict: dict, features: dict | None = None, tmp_path=None) -> dict:
    """実際に app.js を走らせて描画結果を得る。"""
    tmp = Path(tmp_path or ROOT / "out")
    tmp.mkdir(parents=True, exist_ok=True)
    pf = tmp / "_predict.json"
    pf.write_text(json.dumps(predict, ensure_ascii=False), encoding="utf-8")
    args = ["node", str(ROOT / "tests" / "dom_render.js"), str(pf)]
    if features is not None:
        ff = tmp / "_features.json"
        ff.write_text(json.dumps(features, ensure_ascii=False), encoding="utf-8")
        args.append(str(ff))
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                         cwd=str(ROOT), timeout=60)
    assert res.returncode == 0, f"描画に失敗: {res.stderr[-2000:]}"
    return json.loads(res.stdout)


def _render_build_free(predict: dict, tmp_path=None) -> dict:
    """予想画面を描画して買い目ブロックも取り出す (features は本番カタログ)。"""
    return _render(predict, api.feature_catalog(), tmp_path)


def _render_build(features: dict, tmp_path=None) -> dict:
    """作成画面 (STEP1 + 詳細設定) を実際に描画する。

    A-2 の再発防止: 予想画面だけを描画していたため、作成画面の
    `<button class="pchip">` の中に `<button>` を置いた実装を検出できなかった。
    """
    tmp = Path(tmp_path or ROOT / "out")
    tmp.mkdir(parents=True, exist_ok=True)
    ff = tmp / "_features_build.json"
    ff.write_text(json.dumps(features, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(["node", str(ROOT / "tests" / "dom_render.js"),
                          "--build", str(ff)],
                         capture_output=True, text=True, encoding="utf-8",
                         cwd=str(ROOT), timeout=60)
    assert res.returncode == 0, f"描画に失敗: {res.stderr[-2000:]}"
    return json.loads(res.stdout)


def _predict_fixture(n=12, finished=False, scramble=False) -> dict:
    """実 API と同じ形の予想レスポンス (predict_service が返す形)。

    `scramble=True` で **評価順と馬番順が食い違う**ようにする。既定の値割り当ては
    特徴量の向き (斤量・着順はどちらも小さいほど良い) の都合で評価順が馬番順と
    一致してしまい、「馬番順に並べているか」を検査できなかった。
    """
    horses = []
    for k in range(1, n + 1):
        # 馬番と評価の対応を崩す (7 は n と互いに素になりやすい選び方)
        v = float((k * 7) % n + 1) if scramble else float(k)
        horses.append({
            "num": f"{k:02d}", "name": f"馬{k}", "waku": min(8, k),
            "order": (k if finished else 0), "odds": 2.0 + k, "pop": k,
            "n_past_runs": 6,
            "x": {"burden_weight": v, "agg_avg_finish|lb=3|m=": v},
        })
    race = {"race_id": "R1", "date": "20260726", "race_name": "テストレース",
            "race_num": "07", "start_time": "15:45", "seg": {}, "trusted": True,
            "odds_as_of": "2026-07-26T15:31:07", "tan": {"01": 300},
            "horses": horses, "weight_announced": True}
    cfg = {"step1": ["burden_weight"],
           "step2": [{"metric": "agg_avg_finish", "match": [], "lookback": 3}]}
    preset = {"weights": {"burden_weight": 1.0, "agg_avg_finish|lb=3|m=": 2.0},
              "confidence_thresholds": {"solid": 1.0, "strong": 0.3,
                                        "scale": svc.ps.CONFIDENCE_SCALE}}
    return svc.predict_race(race, cfg, preset)


# ---------------------------------------------------------------------------
# F1: button の入れ子と .why の親子関係
# ---------------------------------------------------------------------------
def test_no_button_is_nested_inside_another_button(tmp_path):
    """回帰テスト: button の入れ子を作らないこと。

    入れ子にするとパーサが内側 button 以降を外へ吐き出し、
    行の中身が壊れる (実際に起きた)。
    """
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    for name in ("markList", "raceHead", "markLegend"):
        t = Tree()
        t.feed(out[name])
        assert t.max_button_depth <= 1, f"{name} で button が入れ子になっている"


def test_why_card_is_a_child_of_the_horse_row(tmp_path):
    """`.why` が `.horse` の子孫であること。

    CSS は `.horse.open .why` で開くので、親子が崩れると
    「タップしても何も出ない」状態になる。
    """
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    horses = t.find("horse")
    whys = t.find("why")
    assert horses, "馬の行が描画されていない"
    assert len(whys) == len(horses), (len(whys), len(horses))
    for w in whys:
        assert any("horse" in a["cls"] for a in t.ancestors(w)), \
            ".why が .horse の子孫になっていない"


def test_row_holds_the_odds_and_popularity(tmp_path):
    """`.sub` に人気とオッズが入っていること (行の外へ脱落しない)。"""
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    subs = t.find("sub")
    assert subs, ".sub が無い"
    txt = t.all_text(subs[0])
    assert "番人気" in txt, txt
    assert "単勝" in txt, txt
    # .sub は .row の子孫
    assert any("row" in a["cls"] for a in t.ancestors(subs[0]))


def test_row_is_keyboard_operable_without_being_a_button(tmp_path):
    """行は button ではなく role=button + tabindex で操作可能にすること。"""
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    rows = t.find("row")
    assert rows
    for r in rows:
        assert r["tag"] != "button", "行が button のままだと用語ボタンを入れられない"
        assert r["attrs"].get("role") == "button", r["attrs"]
        assert r["attrs"].get("tabindex") == "0", r["attrs"]
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "keydown" in js and "'Enter'" in js, "Enter/Space の処理が無い"


def test_term_buttons_survive_inside_the_row(tmp_path):
    """用語ボタンが行の中に残っていること (吐き出されていない)。"""
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    terms = t.find("term")
    assert terms, "用語ボタンが無い"
    inside = [x for x in terms if any("row" in a["cls"] for a in t.ancestors(x))]
    assert inside, "用語ボタンが行の外に出ている"
    assert all(x["attrs"].get("data-term") for x in inside)


# ---------------------------------------------------------------------------
# F2: 枠番はサーバの値を使う
# ---------------------------------------------------------------------------
def test_waku_class_matches_the_server_value(tmp_path):
    """枠色クラス w1〜w8 が API の waku と一致すること (馬番から計算しない)。"""
    for n in (8, 12, 16):
        p = _predict_fixture(n=n)
        want = {m["horse_num"]: m["waku"] for m in p["marks"]}
        out = _render(p, api.feature_catalog(), tmp_path)
        t = Tree()
        t.feed(out["markList"])
        for chip in t.find("waku"):
            num = int(t.all_text(chip).strip())
            key = f"{num:02d}"
            cls = [c for c in chip["cls"] if c.startswith("w") and c != "waku"]
            assert cls, chip["cls"]
            assert cls[0] == f"w{want[key]}", (n, num, cls[0], want[key])


def test_missing_waku_renders_without_colour(tmp_path):
    """枠番が無いときは色を付けない (誤った色より無色)。"""
    p = _predict_fixture(n=8)
    for m in p["marks"]:
        m["waku"] = None
    out = _render(p, api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    for chip in t.find("waku"):
        assert "w-none" in chip["cls"], chip["cls"]
        assert not any(c in chip["cls"] for c in [f"w{i}" for i in range(1, 9)])


# ---------------------------------------------------------------------------
# F5: 終了レースには印を出さない
# ---------------------------------------------------------------------------
def test_finished_race_shows_no_marks(tmp_path):
    """機械検査: finished なレースの描画に .mark が現れないこと。"""
    p = _predict_fixture(finished=True)
    assert p["finished"] is True
    out = _render(p, api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    assert t.find("mark") == [], "終了レースに印が出ている"
    assert t.find("horse") == []
    assert "終了しています" in t.all_text(t.root)
    # 結果が出ている
    assert t.find("res-row"), "結果が表示されていない"
    # 凡例も出さない
    assert out["markLegend"] == ""


def test_upcoming_race_shows_marks_and_legend(tmp_path):
    p = _predict_fixture(finished=False)
    out = _render(p, api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    marks = t.find("mark")
    assert marks, "発走前レースに印が出ていない"
    assert "◎" in t.all_text(marks[0])
    assert out["markLegend"], "印の凡例が無い"


# ---------------------------------------------------------------------------
# F11: 項目別カバレッジが寄与行に出る
# ---------------------------------------------------------------------------
def test_item_level_coverage_appears_in_the_breakdown(tmp_path):
    """複数項目選択時、値があった頭数が寄与行に出ること。"""
    p = _predict_fixture(n=10)
    # 1項目だけ一部の馬に値が無い状態を作る
    for m in p["marks"]:
        for c in m["contributions"]:
            if c["id"].startswith("agg_"):
                c["n_with_value"] = 6
                c["n_runners"] = 10
    out = _render(p, api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    txt = t.all_text(t.find("why")[0])
    assert "6/10頭" in txt, txt
    # 馬単位の参照走数も残っている (別の軸として両方出す)
    assert "過去 6走" in txt or "過去 6走を参照" in txt, txt


def test_breakdown_has_no_negative_percentage(tmp_path):
    """負のパーセントが描画結果に現れないこと (見出しで向きを示す)。"""
    import re
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    txt = t.all_text(t.root)
    assert not re.search(r"[−-]\s*\d+\s*%", txt), txt[:300]


def test_decisive_sentence_is_rendered(tmp_path):
    """決め手の一文が根拠カードの先頭に出ること。"""
    out = _render(_predict_fixture(), api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["markList"])
    d = t.find("decisive")
    assert d, "決め手の一文が描画されていない"
    assert "「" in t.all_text(d[0])


# ---------------------------------------------------------------------------
# A-2: 作成画面でも操作可能要素を入れ子にしない
# ---------------------------------------------------------------------------
def test_build_screen_has_no_nested_interactive_elements(tmp_path):
    """項目チップの中に button / input を置かないこと。

    以前は `<button class="pchip">` の中に ⓘ ボタンと「データ少なめ」チップを
    入れていた (22箇所)。HTML の内容モデル違反で、パーサが吐き出す環境では
    表示が崩れ、そうでない環境でもスクリーンリーダーとタップ判定が壊れる。
    """
    out = _render_build(api.feature_catalog(), tmp_path)
    assert out["step1groups"], "STEP1 が描画されていない"
    for name in ("step1groups", "step2list", "starterBox"):
        t = Tree()
        t.feed(out[name])
        assert t.max_button_depth <= 1, f"{name} で button が入れ子になっている"
        # button の中に input (チェックボックス・スライダー) も置かない
        for node in t.find_tag("input"):
            assert not any(a["tag"] == "button" for a in t.ancestors(node)), \
                f"{name} で button の中に input がある"


def test_info_button_is_a_sibling_of_the_chip(tmp_path):
    """ⓘ はチップの兄弟であること (チップの子だと入れ子になる)。"""
    out = _render_build(api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["step1groups"])
    chips = t.find("pchip")
    infos = t.find("pinfo")
    assert chips, "項目チップが無い"
    assert infos, "説明ボタンが無い"
    for i in infos:
        assert not any("pchip" in a["cls"] for a in t.ancestors(i)), \
            "ⓘ がチップの子になっている"
        assert any("pchip-wrap" in a["cls"] for a in t.ancestors(i))


def test_low_sample_mark_is_not_interactive(tmp_path):
    """「データ少なめ」の印は操作不可 (説明は隣の ⓘ に寄せる)。

    `low_sample` はプリセットの `low_sample_columns` から付くので、
    `api.feature_catalog()` をそのまま使うと **他のテストが置いた合成プリセット**に
    左右される (単独では通るのに全体実行で落ちた)。ここは印の構造だけを見たいので、
    カタログに明示的に低サンプル項目を1つ作る。
    """
    cat = api.feature_catalog()
    cat["step1"] = [dict(x) for x in cat["step1"]]
    cat["step1"][0]["low_sample"] = True
    cat["step1"][0]["min_train_races"] = 53
    out = _render_build(cat, tmp_path)
    t = Tree()
    t.feed(out["step1groups"])
    marks = t.find("thin-mark")
    assert marks, "低サンプルの印が無い"
    for m in marks:
        assert m["tag"] == "span", m["tag"]
        assert "data-term" not in m["attrs"]
        # 印そのものは操作不可。説明は同じチップの隣の ⓘ が担う
        wrap = next(a for a in t.ancestors(m) if "pchip-wrap" in a["cls"])
        assert t.find("pinfo", wrap), "低サンプル項目に説明ボタンが無い"


# ---------------------------------------------------------------------------
# C-2: 期間はスライダー1つ (11チップを並べない)
# ---------------------------------------------------------------------------
def test_lookback_is_a_slider_not_eleven_chips(tmp_path):
    """9項目 × 11 = 99個のチップを並べないこと。"""
    cat = api.feature_catalog()
    n_metrics = len(cat["step2_metrics"])
    n_lookbacks = len(cat["step2_lookbacks"])
    assert n_lookbacks >= 11, "期間の選択肢が減っていたらこのテストの前提が変わる"
    out = _render_build(cat, tmp_path)
    t = Tree()
    t.feed(out["step2list"])
    ranges = [x for x in t.find_tag("input")
              if x["attrs"].get("type") == "range"]
    assert len(ranges) == n_metrics, (len(ranges), n_metrics)
    for r in ranges:
        assert r["attrs"].get("aria-label"), "スライダーにラベルが無い"
        assert r["attrs"].get("max") == "10", r["attrs"]
    # 期間のチップは「区切る/全走も使う」の2つだけ (11択を並べない)
    chips = t.find("cchip")
    kinds = {}
    for c in chips:
        kinds[c["attrs"].get("data-kind")] = kinds.get(c["attrs"].get("data-kind"), 0) + 1
    assert kinds.get("match") == n_metrics * len(cat["step2_matches"]), kinds
    assert kinds.get("lbuse") == n_metrics, kinds
    assert kinds.get("lball") == n_metrics, kinds
    assert set(kinds) == {"match", "lbuse", "lball"}, kinds
    # 押した状態は aria-pressed で伝える (見た目のクラスだけにしない)
    assert all(c["attrs"].get("aria-pressed") is not None for c in chips)


def test_cell_count_is_visible(tmp_path):
    """直積で生成されるセル数の表示先があること (規模を隠さない)。"""
    out = _render_build(api.feature_catalog(), tmp_path)
    t = Tree()
    t.feed(out["step2list"])
    notes = t.find("cellnote")
    assert len(notes) == len(api.feature_catalog()["step2_metrics"])
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "通りの組み合わせ" in js, "セル数の文言が無い"
    assert "function totalCells" in js


# ---------------------------------------------------------------------------
# 公式サイトへの引き渡し (誤登録の最後の防波堤)
# ---------------------------------------------------------------------------
def test_handoff_lists_every_bet_type_with_its_count(tmp_path):
    """券種ごとの行と点数、および合計点が出ていること。

    QR は JRA 公式サイトでしか作れず、外部から買い目を渡す口も無い
    (調査記録: docs/SMAPPY_QR_PLAN.md)。人が手で入れるしかないので、
    **入れ終わったあとに突き合わせる材料**が必ず画面に無いといけない。
    """
    p = _predict_fixture(n=12)
    assert p["bet_slip"], "買い目が生成されていない"
    out = _render_build_free(p, tmp_path)
    t = Tree()
    t.feed(out["betSlip"] + out["betResult"])
    items = t.find("hb-item")
    assert len(items) == len(p["bet_slip"]), (len(items), len(p["bet_slip"]))
    total = sum(x["n"] for x in p["bet_slip"])
    txt = t.all_text(t.root)
    assert f"全{total}点" in txt, txt[:200]
    # 読み合わせの指示があること (突き合わせずに買わせない)
    assert t.find("hb-check"), "読み合わせの案内が無い"
    assert "読み合わせ" in txt


def test_handoff_keeps_the_direction_of_ordered_bets(tmp_path):
    """引き渡しの一覧でも馬単の向きが残ること (`-` に潰れない)。"""
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betSlip"] + out["betResult"])
    rows = {}
    for li in t.find("hb-item"):
        label = t.all_text(t.find("hb-t", li)[0]).strip()
        rows[label] = t.all_text(t.find("hb-c", li)[0]).strip()
    assert "→" in rows["馬単"], rows["馬単"]
    assert "→" not in rows["馬連"], rows["馬連"]
    # 同じ2頭が馬連と馬単で違う文字列になること (券種の違いが読める)
    assert rows["馬連"] != rows["馬単"]


def test_handoff_has_no_nested_interactive_elements(tmp_path):
    """<details>/<summary> の中に button を入れ子で置かないこと。"""
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betSlip"] + out["betResult"])
    assert t.max_button_depth <= 1, "買い目ブロックで button が入れ子になっている"
    for node in t.find_tag("input"):
        assert not any(a["tag"] == "button" for a in t.ancestors(node))


# ---------------------------------------------------------------------------
# 買い目エディタ (券種 → 買い方 → 馬(枠))
# ---------------------------------------------------------------------------
def test_all_bet_types_are_offered_as_buttons(tmp_path):
    """8券種すべてがボタンとして並ぶこと (選択肢を隠さない)。"""
    cat = api.feature_catalog()
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betDraft"])
    btns = t.find("btype")
    assert [b["attrs"]["data-btype"] for b in btns] == [x["key"] for x in cat["bet_types"]]
    assert len(btns) == 8, len(btns)
    on = [b for b in btns if "on" in b["cls"]]
    assert len(on) == 1, "選択中の券種は1つ"


def test_the_modes_of_the_selected_type_are_offered(tmp_path):
    """選択中の券種が持つ買い方がすべて出ること。文言は API 経由。"""
    cat = api.feature_catalog()
    first = cat["bet_types"][0]
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betDraft"])
    modes = t.find("bmode")
    assert [m["attrs"]["data-bmode"] for m in modes] == [x["key"] for x in first["modes"]]
    assert [t.all_text(m).strip() for m in modes] == [x["label"] for x in first["modes"]]


def test_the_input_rows_come_from_the_server(tmp_path):
    """入力欄の数と見出しはサーバの仕様どおりであること。

    段数を UI が決めると、フォーメーションの段数が2箇所に散る。
    """
    cat = api.feature_catalog()
    first = cat["bet_types"][0]
    mode = first["modes"][0]
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betDraft"])
    boxes = [x for x in t.find("bed-chips")]
    assert len(boxes) == len(mode["groups"]), (len(boxes), len(mode["groups"]))
    txt = t.all_text(t.root)
    for g in mode["groups"]:
        assert g["label"] in txt, g["label"]


def test_every_runner_can_be_picked_and_is_in_number_order(tmp_path):
    """印が付いていない馬も選べること。並びは馬番順。

    印は「選んだ項目での相対順位」でしかなく推奨ではない。5頭に制限すると
    印を権威として扱うことになる。`marks` は評価順なので並べ替えが必要。
    """
    p = _predict_fixture(n=12, scramble=True)
    # このテストが空振りしないための前提: marks は馬番順になっていないこと
    ranked = [int(m["horse_num"]) for m in p["marks"]]
    assert ranked != sorted(ranked), "fixture の評価順が馬番順と一致している"
    out = _render_build_free(p, tmp_path)
    t = Tree()
    t.feed(out["betDraft"])
    chips = [c for c in t.find("bchip") if c["attrs"].get("data-num")]
    assert len(chips) == len(p["marks"]), (len(chips), len(p["marks"]))
    got = [int(c["attrs"]["data-num"]) for c in chips]
    assert got == sorted(got), got
    # 押した状態は aria-pressed で伝える
    assert all(c["attrs"].get("aria-pressed") in ("true", "false") for c in chips)
    # 既定では何も選ばれていない (券種を選んでから馬を選ぶ流れ)
    assert not any("on" in c["cls"] for c in chips)


def test_marks_are_shown_on_the_chips(tmp_path):
    """印はチップの中に残す (◎がどれか分かること)。"""
    p = _predict_fixture(n=12)
    out = _render_build_free(p, tmp_path)
    t = Tree()
    t.feed(out["betDraft"])
    txt = t.all_text(t.root)
    for m in p["marks"]:
        if m.get("mark"):
            assert m["mark"] in txt, m["mark"]


def test_bet_editor_has_no_nested_interactive_elements(tmp_path):
    """チップや行の中に button / select / input を入れ子で置かないこと。"""
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    for name in ("betDraft", "betResult"):
        t = Tree()
        t.feed(out[name])
        assert t.max_button_depth <= 1, f"{name} で button が入れ子になっている"
        for tag in ("input", "select"):
            for node in t.find_tag(tag):
                assert not any(a["tag"] == "button" for a in t.ancestors(node)), (name, tag)


def test_the_ui_does_not_compute_combinations_or_counts(tmp_path):
    """組み合わせと点数はサーバが返した値を出すこと。

    表記を UI で組み立て直して馬単の方向を落とした事故があったので、
    同じ経路で数え間違いも起きないように点数もサーバに寄せている。
    """
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "got.total" in js, "点数をサーバの値から取っていない"
    assert ".combos" not in js, "UI が combos を読んでいる"
    for bad in ("permutations", "combinations", "formationOf", "product("):
        assert bad not in js, bad


def test_the_slip_shows_what_was_selected_per_row(tmp_path):
    """買い目1件ごとに「どの段に何を選んだか」が出ること。

    点の一覧だけだと、フォーメーションで1着候補が何だったのか読み取れない。
    見出しと番号はサーバの `picks` をそのまま出す。
    """
    p = _predict_fixture(n=12)
    out = _render_build_free(p, tmp_path)
    t = Tree()
    t.feed(out["betResult"])
    # `.bs-picks` は買い目リストと読み合わせの両方に出るので、行の中に限る
    dls = [x for x in t.find("bs-picks")
           if any("bs-row" in a["cls"] for a in t.ancestors(x))]
    assert len(dls) == len(p["bet_slip"]), (len(dls), len(p["bet_slip"]))
    for entry, dl in zip(p["bet_slip"], dls):
        dts = [x for x in dl["children"] if x["tag"] == "dt"]
        dds = [x for x in dl["children"] if x["tag"] == "dd"]
        assert len(dts) == len(entry["picks"]) == len(dds), entry["key"]
        for pk, dt, dd in zip(entry["picks"], dts, dds):
            assert t.all_text(dt).strip() == pk["label"]
            assert t.all_text(dd).strip() == pk["text"]


def test_the_point_list_is_collapsed_but_present(tmp_path):
    """点の内訳は畳んでおく。**消してはいけない** (手入力に必要)。"""
    out = _render_build_free(_predict_fixture(n=12), tmp_path)
    t = Tree()
    t.feed(out["betResult"])
    pts = t.find("bs-pts")
    assert pts, "点の内訳が無い"
    for d in pts:
        assert d["tag"] == "details", d["tag"]
        assert "open" not in d["attrs"], "既定で開いていると縦に伸びる"
        assert t.find("bs-v", d), "内訳の中身が無い"


def test_the_handoff_also_shows_what_was_selected(tmp_path):
    """読み合わせの各件にも段ごとの選択を添えること。

    公式画面と突き合わせるとき、点だけでなく「1着候補に誰を入れたか」を
    確かめられる必要がある。
    """
    p = _predict_fixture(n=12)
    out = _render_build_free(p, tmp_path)
    t = Tree()
    t.feed(out["betResult"])
    items = t.find("hb-item")
    assert items
    for entry, li in zip(p["bet_slip"], items):
        dls = t.find("bs-picks", li)
        assert dls, entry["key"]
        got = [x["text"].strip() for x in dls[0]["children"] if x["tag"] == "dt"]
        assert got == [pk["label"] for pk in entry["picks"]], entry["key"]
