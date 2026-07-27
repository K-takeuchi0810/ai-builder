"""印から買い目 (券種と馬番の組) を組み立てる。

## 何をして、何をしないか

**する**: 印 (◎○▲△×) を券種ごとの馬番の組に並べ替える。
**しない**:

- 金額は一切扱わない (何円買うかは組み立てない)
- QR コードを生成しない。**スマッピー投票の QR データ形式は非公開**で、
  JRA 公式の生成サイト (qrcode.jra.go.jp) だけが正規の経路。形式を推測して
  作ると、読めないか **間違った馬券を実際のお金で登録する** 危険がある。
  ここで出すのは公式サイトに手入力するための一覧。
- 「儲かる」方向の言葉を出さない (設計書 v0.3 §1 DON'T)

## 前提

印は「選んだ項目での相対順位」でしかない。実測で ◎ の的中率は約20%
(1番人気は33%)、回収率は控除率に収束する。買い目は **印の並べ替えであって
推奨ではない** — この一文を UI が必ず添える (テストで固定)。
"""

from __future__ import annotations

# 券種の定義。印の位置 (0=◎ 1=○ 2=▲ 3=△ 4=×) から組を作る。
# 事前固定の素直な組み合わせだけを持つ (点数が爆発する買い方は作らない)。
BET_TYPES: tuple[dict, ...] = (
    {"key": "tan", "label": "単勝", "desc": "1着になる馬を1頭選ぶ"},
    {"key": "fuku", "label": "複勝", "desc": "3着以内に入る馬を1頭選ぶ"},
    {"key": "umaren", "label": "馬連", "desc": "1着と2着の組(順序は問わない)"},
    {"key": "wide", "label": "ワイド", "desc": "3着以内に2頭とも入る組"},
    {"key": "umatan", "label": "馬単", "desc": "1着と2着を順序どおりに当てる"},
    {"key": "sanrenpuku", "label": "三連複", "desc": "3着までの3頭の組(順序は問わない)"},
)


def _nums(marks: list[dict]) -> list[str]:
    """印が付いた馬の馬番を印の順に (◎○▲△×)。無印は含めない。"""
    return [m["horse_num"] for m in marks if m.get("mark")]


def build(marks: list[dict]) -> list[dict]:
    """印 → 券種ごとの買い目。印が2頭未満なら組み合わせ系は作らない。

    返り値の各要素: {"key", "label", "desc", "combos": [[馬番, ...], ...], "n"}
    """
    n = _nums(marks)
    if not n:
        return []
    hon = n[0]
    combos: dict[str, list[list[str]]] = {
        "tan": [[hon]],
        "fuku": [[hon]],
        # ◎ から他の印へ流す (◎を軸にした素直な組み方)
        "umaren": [[hon, x] for x in n[1:]],
        "wide": [[hon, x] for x in n[1:]],
        "umatan": [[hon, x] for x in n[1:]],       # ◎を1着に固定
        "sanrenpuku": _trios(n),
    }
    out = []
    for t in BET_TYPES:
        c = [x for x in combos.get(t["key"], []) if len(set(x)) == len(x)]
        if not c:
            continue
        out.append({**t, "combos": c, "n": len(c)})
    return out


def _trios(n: list[str]) -> list[list[str]]:
    """◎ を含む3頭の組。印が3頭未満なら作らない。"""
    if len(n) < 3:
        return []
    hon, rest = n[0], n[1:]
    out = []
    for i in range(len(rest)):
        for j in range(i + 1, len(rest)):
            out.append([hon, rest[i], rest[j]])
    return out


def as_text(slip: list[dict]) -> str:
    """公式サイトへ手入力するための平文。金額は含めない。"""
    lines = []
    for t in slip:
        for c in t["combos"]:
            lines.append(f"{t['label']} {'-'.join(str(int(x)) for x in c)}")
    return "\n".join(lines)
