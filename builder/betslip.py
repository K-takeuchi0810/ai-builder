"""印から買い目 (券種と馬番の組) を組み立てる。

## 何をして、何をしないか

**する**: 参加者が選んだ「使う馬・軸・券種ごとの組み方」から馬番の組を作る。
既定は印 (◎○▲△×) の並べ替えだが、**参加者が自分で決められる**。
**しない**:

- 金額は一切扱わない (何円買うかは組み立てない)
- QR コードを生成しない。**スマッピー投票の QR データ形式は非公開**で、
  JRA 公式の生成サイト (qrcode.jra.go.jp) だけが正規の経路。形式を推測して
  作ると、読めないか **間違った馬券を実際のお金で登録する** 危険がある。
  (調査記録: `docs/SMAPPY_QR_PLAN.md`)
- 「儲かる」方向の言葉を出さない (設計書 v0.3 §1 DON'T)
- **どの買い方が有利かを示唆しない。** 点数だけを出す

## 前提

印は「選んだ項目での相対順位」でしかない。実測で ◎ の的中率は約20%
(1番人気は33%)、回収率は長期では 1−控除率 (約80%) が上限。買い目は
**印の並べ替えであって推奨ではない** — この一文を UI が必ず添える (テストで固定)。

## 2つの不変条件

1. **表記の正本はここ。** 券種ごとの区切り (馬連は `-`、馬単は `→`) は
   `combo_text` だけが決める。UI 側で組み立て直すと、**手入力する当人が見る文字列**で
   馬単の方向が消え、違う馬券を買うことになる (実際に一度そうなった)。
2. **黙って別の買い目を作らない。** 指定が組めない券種は捨てるのではなく
   `skipped` に理由を入れて返す。組めなかったことが画面から分かる必要がある。
"""

from __future__ import annotations

from itertools import combinations, permutations

# 券種の定義。
#   size    … 1点に必要な頭数
#   ordered … 着順が決まっているか (表記に矢印を使う)
#   modes   … 選べる組み方 (先頭が既定)
BET_TYPES: tuple[dict, ...] = (
    {"key": "tan", "label": "単勝", "desc": "1着になる馬を1頭選ぶ",
     "size": 1, "modes": ("jiku", "each")},
    {"key": "fuku", "label": "複勝", "desc": "3着以内に入る馬を1頭選ぶ",
     "size": 1, "modes": ("jiku", "each")},
    {"key": "umaren", "label": "馬連", "desc": "1着と2着の組(順序は問わない)",
     "size": 2, "modes": ("nagashi", "box")},
    {"key": "wide", "label": "ワイド", "desc": "3着以内に2頭とも入る組",
     "size": 2, "modes": ("nagashi", "box")},
    # C-3: 馬連「11-10」と馬単「11-10」が同じ文字列だと券種の違いを誤学習する。
    # 順序固定の券種は矢印で方向を示す (ordered=True)。
    {"key": "umatan", "label": "馬単", "desc": "1着と2着を順序どおりに当てる",
     "ordered": True, "size": 2,
     "modes": ("nagashi_1st", "nagashi_2nd", "nagashi_both", "box")},
    {"key": "sanrenpuku", "label": "三連複", "desc": "3着までの3頭の組(順序は問わない)",
     "size": 3, "modes": ("nagashi", "nagashi2", "box")},
)
BY_KEY = {t["key"]: t for t in BET_TYPES}

# その組み方に必要な軸の頭数 (None = 軸を使わない)
AXIS_SIZE: dict[str, int | None] = {
    "jiku": 1, "each": None, "box": None, "nagashi": 1, "nagashi2": 2,
    "nagashi_1st": 1, "nagashi_2nd": 1, "nagashi_both": 1,
}
# 軸に選べる上限 (三連複の軸2頭流しがあるため2)
MAX_AXIS = 2


class SelectionError(ValueError):
    """参加者の指定が組めない形のとき。**黙って別の買い目を作らない。**"""


def combo_text(bet_type: dict, combo: list[str]) -> str:
    """1点の表記。順序固定の券種は「11→10」、それ以外は「2-11」。

    順序が意味を持たない券種は **馬番順に並べる**。公式サイトの入力は馬番順の
    マス目なので、「11-2」のように軸を先に出すと転記でずれる。
    順序固定の券種 (馬単) は並べ替えない — 並び自体が着順の指定なので。
    """
    if bet_type.get("ordered"):
        return "→".join(str(int(x)) for x in combo)
    return "-".join(str(int(x)) for x in sorted(combo, key=_num))


def _num(x) -> int:
    return int(str(x))


def _ordered_nums(nums) -> list[str]:
    """馬番順 (文字列のままだと "10" < "2" になる)。重複は落とす。"""
    return sorted(dict.fromkeys(str(x) for x in nums), key=_num)


def _marked(marks: list[dict]) -> list[str]:
    """印が付いた馬の馬番を印の順に (◎○▲△×)。無印は含めない。"""
    return [m["horse_num"] for m in marks if m.get("mark")]


# ---------------------------------------------------------------------------
# 既定の選択 (印の並べ替え) — 参加者が何も触らなければこれになる
# ---------------------------------------------------------------------------
def default_selection(marks: list[dict]) -> dict:
    """◎を軸、○▲△×を相手にした素直な組み方。

    返り値は `build_custom` にそのまま渡せる形。参加者はここから変えていく。
    """
    n = _marked(marks)
    if not n:
        return {"horses": [], "axis": [], "modes": {}}
    return {
        "horses": _ordered_nums(n),
        "axis": n[:1],                      # ◎
        "modes": {t["key"]: t["modes"][0] for t in BET_TYPES},
    }


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def build(marks: list[dict]) -> list[dict]:
    """印 → 券種ごとの買い目 (既定の組み方)。"""
    slip, _ = build_custom(default_selection(marks))
    return slip


def build_custom(selection: dict,
                 *, runners: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """参加者の指定 → (買い目, 組めなかった券種と理由)。

    selection = {
      "horses": ["10","11",...],   # 使う馬 (軸を含む)
      "axis":   ["11"],            # 軸 (三連複の軸2頭流しは2頭)
      "modes":  {"umaren": "nagashi", "umatan": "box", "wide": None, ...}
    }
    `modes` の値が None / 未指定の券種は作らない (OFF)。

    `runners` を渡すと **出走していない馬番を弾く**。これは全体の誤りなので例外。
    券種ごとの不整合 (軸の頭数が足りない等) は例外にせず `skipped` に入れる
    — 1券種の指定違いで他の券種まで消えると、何が起きたのか分からなくなる。
    """
    horses = _ordered_nums(selection.get("horses") or [])
    axis = [str(x) for x in (selection.get("axis") or [])]
    modes = selection.get("modes") or {}

    if runners is not None:
        allowed = {str(x) for x in runners}
        bad = sorted({x for x in horses + axis if x not in allowed}, key=_num)
        if bad:
            raise SelectionError(
                "出走していない馬番が指定されています: "
                + "・".join(str(_num(x)) for x in bad))
    outside = [a for a in axis if a not in horses]
    if outside:
        raise SelectionError(
            "軸の馬が「使う馬」に入っていません: "
            + "・".join(str(_num(x)) for x in outside))
    if len(axis) > MAX_AXIS:
        raise SelectionError(f"軸は{MAX_AXIS}頭までです(いまは{len(axis)}頭)")

    slip: list[dict] = []
    skipped: list[dict] = []
    for t in BET_TYPES:
        mode = modes.get(t["key"])
        if not mode:
            continue
        if mode not in t["modes"]:
            skipped.append({"key": t["key"], "label": t["label"], "mode": mode,
                            "reason": f"{t['label']}に「{mode}」という組み方はありません"})
            continue
        try:
            combos = _combos(t, mode, horses, axis)
        except SelectionError as err:
            skipped.append({"key": t["key"], "label": t["label"], "mode": mode,
                            "reason": str(err)})
            continue
        if not combos:
            skipped.append({"key": t["key"], "label": t["label"], "mode": mode,
                            "reason": f"{t['label']}には使う馬が{t['size']}頭以上必要です"})
            continue
        if not t.get("ordered"):
            # 表記と同じ並びにする (読み合わせで目が滑らないように)
            combos = [sorted(c, key=_num) for c in combos]
        slip.append({**{k: v for k, v in t.items() if k != "modes"},
                     "mode": mode,
                     "combos": combos,
                     "n": len(combos),
                     "texts": [combo_text(t, c) for c in combos]})
    return slip, skipped


def _combos(t: dict, mode: str, horses: list[str], axis: list[str]) -> list[list[str]]:
    """1券種ぶんの組。軸の頭数が合わなければ SelectionError。"""
    need = AXIS_SIZE.get(mode)
    if need is not None and len(axis) != need:
        return _axis_error(t, mode, need, len(axis))
    partners = [x for x in horses if x not in axis]
    size = t["size"]

    if mode == "jiku":
        return [[axis[0]]]
    if mode == "each":
        return [[x] for x in horses]
    if mode == "box":
        if len(horses) < size:
            return []
        gen = permutations if t.get("ordered") else combinations
        return [list(c) for c in gen(horses, size)]
    if mode == "nagashi":
        if size == 2:
            return [[axis[0], x] for x in partners]
        return [[axis[0], a, b] for a, b in combinations(partners, 2)]
    if mode == "nagashi2":
        return [[axis[0], axis[1], x] for x in partners]
    if mode == "nagashi_1st":
        return [[axis[0], x] for x in partners]
    if mode == "nagashi_2nd":
        return [[x, axis[0]] for x in partners]
    if mode == "nagashi_both":
        return ([[axis[0], x] for x in partners]
                + [[x, axis[0]] for x in partners])
    raise SelectionError(f"未知の組み方: {mode}")


def _axis_error(t: dict, mode: str, need: int, got: int):
    """軸の頭数が合わないとき。**直し方まで書く** — 理由だけだと手が止まる。"""
    from . import labels as lbl
    raise SelectionError(
        f"{t['label']}の「{lbl.bet_mode_label(mode)}」は軸{need}頭が必要です"
        f"(いま{got}頭)。軸を{need}頭にするか、組み方を変えてください")


def total_points(slip: list[dict]) -> int:
    return sum(t["n"] for t in slip)


def as_text(slip: list[dict]) -> str:
    """公式サイトへ手入力するための平文。金額は含めない。

    表記は `combo_text` を通す。ここで `-` を直接 join すると、
    **手入力する当人が見る文字列**で馬単の方向が消える。
    """
    lines = []
    for t in slip:
        for c in t["combos"]:
            lines.append(f"{t['label']} {combo_text(t, c)}")
    return "\n".join(lines)
