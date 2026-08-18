"""買い目 (券種と馬番の組) を組み立てる。

## 何をして、何をしないか

**する**: 参加者が「券種 → 買い方 → 馬(または枠)」を選んだものを、1点ずつの組に
展開する。買い目は**追加していける**ので、1レースで複数券種を作れる。
金額は1点あたり100円単位で保持する。QRの投票データはここでは作らず、
`smappy.py` が買い目と金額をJRA公式サイトへ送り、JRAが返したデータを使う。
**しない**:

- JRAの非公開な投票用QRデータを推測して作らない
- 「儲かる」方向の言葉を出さない (設計書 v0.3 §1 DON'T)
- **どの券種・どの買い方が有利かを示唆しない。** 点数だけを出す

## 出力は「1点ずつの一覧」

買い方 (ながし・ボックス・フォーメーション) は **点を作るための道具**でしかなく、
出てくるのは個々の組。だから公式サイトで「通常」として1点ずつ入れても同じものが
買える。方式が公式の選択肢と一致していなくても、買えない買い目にはならない。

## 前提

印は「選んだ項目での相対順位」でしかない。実測で ◎ の的中率は約20%
(1番人気は33%)、回収率は長期では 1−控除率 (約80%) が上限。買い目は
**参加者が組むものであって推奨ではない** — この一文を UI が必ず添える。

## 3つの不変条件

1. **表記と点数の正本はここ。** 券種ごとの区切り (馬連は `-`、馬単は `→`) は
   `combo_text` だけが決める。UI 側で組み立て直すと、**利用者が確認する文字列**で
   順序指定が消え、違う馬券を買うことになる (実際に一度そうなった)。
2. **黙って別の買い目を作らない。** 組めない指定は捨てるのではなく `skipped` に
   理由と直し方を入れて返す。
3. **出走していない馬番・存在しない枠は組まない。**
"""

from __future__ import annotations

from itertools import combinations, permutations, product

# ---------------------------------------------------------------------------
# 券種
# ---------------------------------------------------------------------------
#   size    … 1点に必要な数
#   unit    … "horse" は馬番で選ぶ / "frame" は枠番で選ぶ
#   ordered … 着順が決まっているか (表記に矢印を使い、並べ替えない)
#   modes   … 選べる買い方 (先頭が既定)
BET_TYPES: tuple[dict, ...] = (
    {"key": "tan", "label": "単勝", "desc": "1着になる馬を1頭選ぶ",
     "size": 1, "unit": "horse", "modes": ("each",)},
    {"key": "fuku", "label": "複勝", "desc": "3着以内に入る馬を1頭選ぶ",
     "size": 1, "unit": "horse", "modes": ("each",)},
    {"key": "wakuren", "label": "枠連", "desc": "1着と2着の枠の組(順序は問わない)",
     "size": 2, "unit": "frame", "modes": ("nagashi", "box")},
    {"key": "umaren", "label": "馬連", "desc": "1着と2着の組(順序は問わない)",
     "size": 2, "unit": "horse", "modes": ("nagashi", "box", "formation")},
    {"key": "wide", "label": "ワイド", "desc": "3着以内に2頭とも入る組",
     "size": 2, "unit": "horse", "modes": ("nagashi", "box", "formation")},
    # C-3: 馬連「11-10」と馬単「11-10」が同じ文字列だと券種の違いを誤学習する。
    # 順序固定の券種は矢印で方向を示す (ordered=True)。
    {"key": "umatan", "label": "馬単", "desc": "1着と2着を順序どおりに当てる",
     "size": 2, "unit": "horse", "ordered": True,
     "modes": ("nagashi_1st", "nagashi_2nd", "nagashi_both", "box", "formation")},
    {"key": "sanrenpuku", "label": "三連複", "desc": "3着までの3頭の組(順序は問わない)",
     "size": 3, "unit": "horse",
     "modes": ("nagashi", "nagashi2", "box", "formation")},
    {"key": "sanrentan", "label": "三連単", "desc": "1着から3着を順序どおりに当てる",
     "size": 3, "unit": "horse", "ordered": True,
     "modes": ("nagashi_1st", "nagashi_2nd", "nagashi_3rd", "box", "formation")},
)
BY_KEY = {t["key"]: t for t in BET_TYPES}
DEFAULT_AMOUNT_YEN = 100
MAX_AMOUNT_PER_POINT_YEN = 999_900


# ---------------------------------------------------------------------------
# 買い方が必要とする「グループ」
# ---------------------------------------------------------------------------
# 参加者に見せる入力欄はこの定義から作る。**UI が独自に段数を決めない。**
#   (キー, 必要数 or None=1つ以上)
_AXIS = ("axis", 1)
_PARTNER = ("partner", None)
GROUP_SPECS: dict[str, tuple[tuple[str, int | None], ...]] = {
    "each": (("pick", None),),
    "box": (("pick", None),),
    "nagashi": (_AXIS, _PARTNER),
    "nagashi2": (("axis", 2), _PARTNER),
    "nagashi_1st": (_AXIS, _PARTNER),
    "nagashi_2nd": (_AXIS, _PARTNER),
    "nagashi_3rd": (_AXIS, _PARTNER),
    "nagashi_both": (_AXIS, _PARTNER),
}
# フォーメーションは券種の size で段数が変わる (2頭系は2段、3頭系は3段)
_FORMATION = {2: (("p1", None), ("p2", None)),
              3: (("p1", None), ("p2", None), ("p3", None))}


def group_specs(bet_type: dict, mode: str) -> tuple[tuple[str, int | None], ...]:
    """その券種・買い方が必要とするグループ (UI の入力欄と1対1)。"""
    if mode == "formation":
        return _FORMATION[bet_type["size"]]
    return GROUP_SPECS[mode]


class SelectionError(ValueError):
    """参加者の指定が組めない形のとき。**黙って別の買い目を作らない。**"""


# ---------------------------------------------------------------------------
# 表記
# ---------------------------------------------------------------------------
def combo_text(bet_type: dict, combo: list[str]) -> str:
    """1点の表記。順序固定の券種は「11→10」、それ以外は「2-11」。

    順序が意味を持たない券種は **馬番(枠番)順に並べる**。公式サイトの入力は
    番号順のマス目なので、「11-2」のように軸を先に出すと転記でずれる。
    順序固定の券種は並べ替えない — 並び自体が着順の指定なので。
    """
    if bet_type.get("ordered"):
        return "→".join(str(int(x)) for x in combo)
    return "-".join(str(int(x)) for x in sorted(combo, key=_num))


def _num(x) -> int:
    return int(str(x))


def _uniq_sorted(items) -> list[str]:
    """番号順・重複なし (文字列のままだと "10" < "2" になる)。"""
    return sorted(dict.fromkeys(str(x) for x in items), key=_num)


def _marked(marks: list[dict]) -> list[str]:
    """印が付いた馬の馬番を印の順に (◎○▲△×)。無印は含めない。"""
    return [m["horse_num"] for m in marks if m.get("mark")]


# ---------------------------------------------------------------------------
# 既定の買い目 (印の並べ替え) — 参加者が何も触らなければこれになる
# ---------------------------------------------------------------------------
def default_selection(marks: list[dict]) -> list[dict]:
    """◎を軸、他の印を相手にした素直な買い目を並べる。

    返り値は `build_custom` にそのまま渡せる形 (買い目の一覧)。
    """
    n = _marked(marks)
    if not n:
        return []
    hon, rest = n[:1], _uniq_sorted(n[1:])
    base = [{"type": "tan", "mode": "each", "groups": [hon]},
            {"type": "fuku", "mode": "each", "groups": [hon]}]
    if not rest:
        return base
    return base + [
        {"type": "umaren", "mode": "nagashi", "groups": [hon, rest]},
        {"type": "wide", "mode": "nagashi", "groups": [hon, rest]},
        {"type": "umatan", "mode": "nagashi_1st", "groups": [hon, rest]},
        {"type": "sanrenpuku", "mode": "nagashi", "groups": [hon, rest]},
    ]


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def build(marks: list[dict]) -> list[dict]:
    """印 → 既定の買い目。"""
    slip, _ = build_custom(default_selection(marks))
    return slip


def build_custom(selection: list[dict], *, runners: list[str] | None = None,
                 frames: dict[str, int] | None = None
                 ) -> tuple[list[dict], list[dict]]:
    """買い目の指定 → (組めた買い目, 組めなかったものと理由)。

    selection は買い目の一覧。1件は
        {"type": "sanrenpuku", "mode": "formation",
         "groups": [["01","02"], ["03"], ["04","05"]]}

    `runners` / `frames` を渡すと **出走していない馬番・存在しない枠を弾く**。
    1件の指定違いで全体を止めない — 何が起きたのか分からないまま買い目が
    消えるのを避けるため、その1件だけ `skipped` に理由と直し方を入れる。
    """
    if isinstance(selection, dict) or not isinstance(selection, (list, tuple)):
        # 形が違う指定で 500 にしない (API の入力なので壊れた形も来る)
        raise SelectionError("買い目の指定は一覧 (配列) で渡してください")
    slip: list[dict] = []
    skipped: list[dict] = []
    for i, entry in enumerate(selection or []):
        try:
            if not isinstance(entry, dict):
                raise SelectionError("買い目1件の形が正しくありません")
            slip.append(_build_one(entry, runners=runners, frames=frames))
        except SelectionError as err:
            # entry が dict でないこともある (壊れた入力) ので getattr で読む
            key = entry.get("type") if isinstance(entry, dict) else None
            t = BY_KEY.get(str(key))
            skipped.append({"index": i, "key": key,
                            "label": t["label"] if t else (str(key) if key else "買い目"),
                            "mode": entry.get("mode") if isinstance(entry, dict) else None,
                            "reason": str(err)})
    return slip, skipped


def _build_one(entry: dict, *, runners=None, frames=None) -> dict:
    t = BY_KEY.get(str(entry.get("type")))
    if t is None:
        raise SelectionError(f"「{entry.get('type')}」という券種はありません")
    mode = str(entry.get("mode") or "")
    if mode not in t["modes"]:
        raise SelectionError(f"{t['label']}に「{mode}」という買い方はありません")

    specs = group_specs(t, mode)
    raw = entry.get("groups") or []
    if len(raw) != len(specs):
        raise SelectionError(
            f"{t['label']}の「{mode_label(mode)}」は{len(specs)}組の指定が必要です"
            f"(いま{len(raw)}組)")
    groups = [_uniq_sorted(g) for g in raw]

    allowed = _allowed(t, runners, frames)
    if allowed is not None:
        bad = _uniq_sorted({x for g in groups for x in g if x not in allowed})
        if bad:
            what = "枠" if t["unit"] == "frame" else "馬番"
            raise SelectionError(
                f"このレースに無い{what}が指定されています: "
                + "・".join(str(_num(x)) for x in bad))

    unit = "枠" if t["unit"] == "frame" else "頭"
    for g, (name, need) in zip(groups, specs):
        if not g:
            raise SelectionError(f"「{group_label(t, mode, name)}」を選んでください")
        if need is not None and len(g) != need:
            raise SelectionError(
                f"「{group_label(t, mode, name)}」は{need}{unit}"
                f"にしてください(いま{len(g)}{unit})")

    combos = _combos(t, mode, groups, frames)
    if not combos:
        raise SelectionError(_why_empty(t, mode, groups))
    if not t.get("ordered"):
        combos = [sorted(c, key=_num) for c in combos]
    combos = _dedup(combos)
    raw_amounts = entry.get("amounts_yen")
    if raw_amounts is None:
        amount_yen = _amount_yen(entry.get("amount_yen", DEFAULT_AMOUNT_YEN))
        amounts_yen = [amount_yen] * len(combos)
    else:
        if not isinstance(raw_amounts, list) or len(raw_amounts) != len(combos):
            raise SelectionError("点別金額は買い目の点数と同じ件数で指定してください")
        amounts_yen = [_amount_yen(raw) for raw in raw_amounts]
        amount_yen = amounts_yen[0] if len(set(amounts_yen)) == 1 else None
    return {"key": t["key"], "label": t["label"], "desc": t["desc"],
            "size": t["size"], "unit": t["unit"],
            "ordered": bool(t.get("ordered")), "mode": mode,
            "mode_label": mode_label(mode),
            "groups": groups,
            # **何を選んだのか**を段ごとに読める形で返す。点の一覧だけだと
            # 「1着に誰を入れたか」がフォーメーションで追えない。
            "picks": _picks(t, mode, specs, groups),
            "combos": combos, "n": len(combos), "amount_yen": amount_yen,
            "amounts_yen": amounts_yen,
            "subtotal_yen": sum(amounts_yen),
            "texts": [combo_text(t, c) for c in combos]}


def _amount_yen(raw) -> int:
    """1点あたりの金額。スマッピーと同じ100円単位で検査する。"""
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        raise SelectionError("金額は100円単位の数字で入力してください")
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise SelectionError("金額は100円単位の数字で入力してください") from exc
    if amount < 100 or amount > MAX_AMOUNT_PER_POINT_YEN or amount % 100:
        raise SelectionError(
            f"金額は100円から{MAX_AMOUNT_PER_POINT_YEN:,}円まで、100円単位で入力してください")
    return amount


def _picks(t: dict, mode: str, specs, groups: list[list[str]]) -> list[dict]:
    """段ごとの選択内容 (見出し + 番号)。表示の体裁もサーバが決める。"""
    out = []
    for (name, _need), g in zip(specs, groups):
        out.append({"key": name,
                    "label": group_label(t, mode, name),
                    "nums": [_num(x) for x in g],
                    "text": PICK_SEP.join(str(_num(x)) for x in g)})
    return out


# 段の中の区切り。組の区切り (`-` / `→`) と混ざらない記号を使う。
PICK_SEP = "・"


def _allowed(t: dict, runners, frames) -> set[str] | None:
    if t["unit"] == "frame":
        return None if frames is None else {str(k) for k in frames}
    return None if runners is None else {str(x) for x in runners}


def _dedup(combos: list[list[str]]) -> list[list[str]]:
    """同じ組を1点にまとめ、番号順に並べる (フォーメーションで重複が出る)。"""
    seen, out = set(), []
    for c in combos:
        key = tuple(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return sorted(out, key=lambda c: [_num(x) for x in c])


def _combos(t: dict, mode: str, groups: list[list[str]],
            frames: dict[str, int] | None) -> list[list[str]]:
    if mode == "each":
        return [[x] for x in groups[0]]
    if mode == "box":
        return _box(t, groups[0], frames)
    if mode == "formation":
        return _formation(groups)
    # ながし系
    axis = groups[0]
    partners = [x for x in groups[1] if x not in axis]
    if not partners:
        return []
    if t["size"] == 2:
        return _nagashi2(t, mode, axis, partners, frames)
    return _nagashi3(mode, axis, partners)


def _box(t: dict, picks: list[str], frames) -> list[list[str]]:
    size = t["size"]
    gen = permutations if t.get("ordered") else combinations
    out = [list(c) for c in gen(picks, size)] if len(picks) >= size else []
    if t["unit"] == "frame":
        out += _zoro(picks, frames)
    return out


def _zoro(picks: list[str], frames) -> list[list[str]]:
    """枠連のゾロ目 (同じ枠の2頭)。**その枠に2頭以上いるときだけ**成立する。"""
    if not frames:
        return []
    return [[f, f] for f in picks if int(frames.get(str(f), 0)) >= 2]


def _nagashi2(t: dict, mode: str, axis: list[str], partners: list[str],
              frames) -> list[list[str]]:
    a = axis[0]
    if mode == "nagashi":                       # 順序なし (枠連・馬連・ワイド)
        out = [[a, p] for p in partners]
        if t["unit"] == "frame":
            out += _zoro([a], frames)           # 軸枠のゾロ目も流しに含める
        return out
    if mode == "nagashi_1st":
        return [[a, p] for p in partners]
    if mode == "nagashi_2nd":
        return [[p, a] for p in partners]
    if mode == "nagashi_both":
        return [[a, p] for p in partners] + [[p, a] for p in partners]
    raise SelectionError(f"未知の買い方: {mode}")


def _nagashi3(mode: str, axis: list[str], partners: list[str]) -> list[list[str]]:
    if mode == "nagashi":                       # 三連複 軸1頭
        return [[axis[0], a, b] for a, b in combinations(partners, 2)]
    if mode == "nagashi2":                      # 三連複 軸2頭
        return [[axis[0], axis[1], p] for p in partners]
    # 三連単 着順を固定して流す
    slot = {"nagashi_1st": 0, "nagashi_2nd": 1, "nagashi_3rd": 2}.get(mode)
    if slot is None:
        raise SelectionError(f"未知の買い方: {mode}")
    out = []
    for a, b in permutations(partners, 2):
        c = [a, b]
        c.insert(slot, axis[0])
        out.append(c)
    return out


def _formation(groups: list[list[str]]) -> list[list[str]]:
    """各段から1つずつ取る。同じ馬(枠)が重なる組は成立しないので落とす。"""
    out = []
    for c in product(*groups):
        if len(set(c)) != len(c):
            continue
        out.append(list(c))
    return out


def _why_empty(t: dict, mode: str, groups: list[list[str]]) -> str:
    """0点になった理由。**直し方まで書く** — 理由だけだと手が止まる。"""
    unit = "枠" if t["unit"] == "frame" else "頭"
    if mode == "box":
        return (f"{t['label']}のボックスには{t['size']}{unit}以上必要です"
                f"(いま{len(groups[0])}{unit})")
    if mode == "formation":
        return (f"{t['label']}のフォーメーションは、各段から重ならない組を"
                f"作れる必要があります。段の中身を見直してください")
    return (f"{t['label']}の「{mode_label(mode)}」は相手を1{unit}以上"
            f"選んでください(軸と同じものだけでは組めません)")


def total_points(slip: list[dict]) -> int:
    return sum(t["n"] for t in slip)


def total_yen(slip: list[dict]) -> int:
    return sum(t["subtotal_yen"] for t in slip)


def as_text(slip: list[dict]) -> str:
    """コピー・照合用の平文。金額は含めない。

    表記は `combo_text` を通す。ここで `-` を直接 join すると、
    **利用者が確認する文字列**で順序指定が消える。
    """
    lines = []
    for t in slip:
        bt = BY_KEY[t["key"]]
        for c in t["combos"]:
            lines.append(f"{t['label']} {combo_text(bt, c)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ラベル (labels.py が正本。循環 import を避けるため関数越しに引く)
# ---------------------------------------------------------------------------
def mode_label(mode: str) -> str:
    from . import labels as lbl
    return lbl.bet_mode_label(mode)


def group_label(t: dict, mode: str, name: str) -> str:
    from . import labels as lbl
    return lbl.bet_group_label(name, size=t["size"],
                               ordered=bool(t.get("ordered")),
                               unit=t["unit"])


def frames_of(horses: list[dict]) -> dict[str, int]:
    """出走馬 → 枠番ごとの頭数。枠連の候補とゾロ目の判定に使う。"""
    out: dict[str, int] = {}
    for h in horses:
        w = h.get("waku")
        if w in (None, "", 0):
            continue
        out[str(int(w))] = out.get(str(int(w)), 0) + 1
    return out
