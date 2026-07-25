"""生 JV-Data (RA レコード) からコーナー通過順位を復元する。

背景: keiba.db の `horse_races.corner_order_1..4` は **全ゼロ** (2021-2025 の有効率 0.0%)。
keiba-yosou の parser は SE レコードのバイトオフセットを未検証のまま扱っていたため、
安全側に倒して意図的にゼロ化していた (parser.py の「probe で緑化するまで使用禁止」)。

一方 **RA レコードにはコーナー通過順位が文字列で実在する** (実データで確認済):
    corner block = コーナー番号(1) + 周回数(1) + 通過順位(70)  … 4 ブロック
    例: '31(*5,6,8)(2,7)(1,4)3' → コーナー3・周回1・順位 (5,6,8)(2,7)(1,4)3

本モジュールは raw ファイルを **read-only** で読み、馬番→各コーナー通過順位を復元して
ai-builder 側のキャッシュに保存する。**keiba.db には一切書き込まない** (不変条件を維持)。

通過順位の記法:
    ','  区切り (順位が下がる)
    '(...)' 横並び = 同順位扱い
    '-' '=' 大きな差 (順位付けには影響させない。区切りとして扱う)
    ' '  **空白も区切り** (実データに '=12   9' のような表記がある。空白を除去すると
         馬番 12 と 9 が '129' に連結して存在しない馬番になる — 実測で検出したバグ)
    '*'  先頭/注目馬のマーク (順位には無関係なので除去する)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

RA_MIN_LEN = 1270
CORNER_BLOCK_START = 981          # 0-based。コーナー情報 (72 バイト × 4)
CORNER_BLOCK_SIZE = 72
RACE_KEY_SLICE = slice(11, 27)    # 年4+月日4+場2+回2+日2+R2


def parse_passing_order(s: str) -> dict[str, int]:
    """通過順位文字列 → {馬番: 順位}。横並び '()' は同順位。

    例: '(*5,6,8)(2,7)(1,4)3' → 5,6,8=1位 / 2,7=2位 / 1,4=3位 / 3=4位
    """
    s = s.replace("*", "")          # 空白は区切りなので **除去しない**
    if not s.strip():
        return {}
    out: dict[str, int] = {}
    pos = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            j = s.find(")", i)
            if j < 0:
                j = len(s)
            group = s[i + 1:j]
            pos += 1
            for num in re.split(r"[,\-=\s]+", group):
                if num.isdigit():
                    out.setdefault(str(int(num)), pos)
            i = j + 1
        elif ch.isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            pos += 1
            out.setdefault(str(int(s[i:j])), pos)
            i = j
        else:                      # ',' '-' '=' などの区切り
            i += 1
    return out


def parse_ra_corners(rec: bytes) -> dict[int, dict[str, int]]:
    """RA レコード → {コーナー番号: {馬番: 順位}}。空ブロックは無視。"""
    out: dict[int, dict[str, int]] = {}
    for b in range(4):
        start = CORNER_BLOCK_START + b * CORNER_BLOCK_SIZE
        block = rec[start:start + CORNER_BLOCK_SIZE]
        if len(block) < 3:
            continue
        head = block[:2].decode("ascii", "ignore")
        if not head[:1].isdigit():
            continue
        corner_no = int(head[0])
        if corner_no == 0:
            continue
        body = block[2:].decode("shift_jis", "replace")
        order = parse_passing_order(body)
        if order:
            out[corner_no] = order
    return out


def iter_ra_records(path: str | Path):
    """RASW ファイルから (race_key, {コーナー番号: {馬番: 順位}}) を yield する。read-only。"""
    data = Path(path).read_bytes()
    for raw in data.split(b"\r\n"):
        rec = raw.lstrip(b"\x00")          # 実データにヌル埋めが入る
        if len(rec) < RA_MIN_LEN or rec[:2] != b"RA":
            continue
        key = rec[RACE_KEY_SLICE].decode("ascii", "ignore")
        if len(key) != 16 or not key.isdigit():
            continue
        corners = parse_ra_corners(rec)
        if corners:
            yield key, corners


def build_corner_index(raw_dir: str | Path, pattern: str = "RASW*.jvd") -> dict:
    """raw ディレクトリ全体を走査してコーナー通過順位の索引を作る。

    戻り: {race_key: {"1": {馬番: 順位}, ..., "4": {...}}}
    """
    index: dict[str, dict] = {}
    for p in sorted(Path(raw_dir).glob(pattern)):
        for key, corners in iter_ra_records(p):
            slot = index.setdefault(key, {})
            for cno, order in corners.items():
                slot[str(cno)] = order        # 同一レースが複数ファイルにある場合は後勝ち (差分更新)
    return index


def save_corner_index(index: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return out


def load_corner_index(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------------------------------------------------------------------
# 過去走への適用 (集計特徴の素データ)
# ---------------------------------------------------------------------------
def corner_positions(index: dict, race_key: str, horse_num: str) -> dict[str, float | None]:
    """あるレース・ある馬の各コーナー通過順位と、そこからの着順上昇を返す。

    first  … 最初に記録されたコーナーの通過順位
    last   … 最後に記録されたコーナー (通常4角) の通過順位
    gain_first_last … first - last (正 = 道中で押し上げた)
    """
    slot = index.get(race_key) or {}
    got: dict[int, int] = {}
    for cno in ("1", "2", "3", "4"):
        order = slot.get(cno)
        if order and horse_num in order:
            got[int(cno)] = order[horse_num]
    if not got:
        return {"first": None, "last": None, "gain_first_last": None}
    ks = sorted(got)
    first, last = got[ks[0]], got[ks[-1]]
    return {"first": float(first), "last": float(last),
            "gain_first_last": float(first - last)}
