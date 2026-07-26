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


def _predict_fixture(n=12, finished=False) -> dict:
    """実 API と同じ形の予想レスポンス (predict_service が返す形)。"""
    horses = []
    for k in range(1, n + 1):
        horses.append({
            "num": f"{k:02d}", "name": f"馬{k}", "waku": min(8, k),
            "order": (k if finished else 0), "odds": 2.0 + k, "pop": k,
            "n_past_runs": 6,
            "x": {"burden_weight": float(k), "agg_avg_finish|lb=3|m=": float(k)},
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
