"""生 JV-Data (SE レコード) から賞金を復元する。

keiba.db には賞金カラムが存在しない (schema.sql に prize/賞金 相当なし)。しかし SE レコードに
実在するので、raw ファイルを **read-only** で読んで ai-builder 側の索引に保存する。
**keiba.db には一切書き込まない** (不変条件を維持)。

オフセットは実データで経験的に確定した (JRA の「5着まで賞金」ルールで検証済み):

    本賞金   1-based 366, 8 桁, 単位 百円
    収得賞金 1-based 374, 8 桁, 単位 百円

検証結果 (SESW 実ファイル n≈2,300):
    1着 99,455 / 2着 39,671 / 3着 25,209 / 4着 14,949 / 5着 9,891 (単調減少・非ゼロ率100%)
    **6着以降は非ゼロ率 0.0%** ← JRA は5着までしか本賞金を出さない
    1着 中央値 82,000 (百円) = 820 万円、範囲 580 万〜4,300 万円 (実際の JRA 賞金と一致)

外部照合 (docs/evidence/20260726_prize_offset_external_check.md):
    ジャパンカップ2025 索引 500,000,000 円 ↔ 公表「1着賞金5億円」 一致
    東京優駿2026 索引 300,000,000 円 ↔ 公表「3億円」 一致
    比率 100:40:26:15:10 (JRA 標準) に厳密一致

**付加賞・褒賞金は含まない**: ジャパンカップ2025 勝ち馬には褒賞金 300 万米ドル
(約4億6500万円) が別途あるが、索引値はきっちり 500,000,000 円。したがって 366 は
「**本賞金**」のみ。UI・寄与分解では「獲得本賞金」と表記し「賞金総額」と書かないこと。
"""

from __future__ import annotations

import json
from pathlib import Path

SE_MIN_LEN = 550
RACE_KEY_SLICE = slice(11, 27)      # 年4+月日4+場2+回2+日2+R2
HORSE_NUM_SLICE = slice(28, 30)     # 1-based 29,2
HONSHO_POS = 366                    # 1-based, 8 桁, 単位 百円
SHUTOKU_POS = 374                   # 1-based, 8 桁, 単位 百円
FIELD_LEN = 8


def _digits(rec: bytes, pos1: int, ln: int = FIELD_LEN) -> int | None:
    s = rec[pos1 - 1:pos1 - 1 + ln].decode("ascii", "ignore")
    return int(s) if s.isdigit() else None


def parse_se_prize(rec: bytes) -> tuple[str, str, dict] | None:
    """SE レコード → (race_key, 馬番, {"honsho_yen":円, "shutoku_yen":円})。"""
    if len(rec) < SE_MIN_LEN or rec[:2] != b"SE":
        return None
    key = rec[RACE_KEY_SLICE].decode("ascii", "ignore")
    if len(key) != 16 or not key.isdigit():
        return None
    hn_raw = rec[HORSE_NUM_SLICE].decode("ascii", "ignore").strip()
    if not hn_raw.isdigit():
        return None
    honsho = _digits(rec, HONSHO_POS)
    shutoku = _digits(rec, SHUTOKU_POS)
    if honsho is None and shutoku is None:
        return None
    return key, str(int(hn_raw)), {
        "honsho_yen": None if honsho is None else honsho * 100,   # 百円 → 円
        "shutoku_yen": None if shutoku is None else shutoku * 100,
    }


def iter_se_prizes(path: str | Path):
    """SESW ファイルから (race_key, 馬番, 賞金dict) を yield する。read-only。"""
    for raw in Path(path).read_bytes().split(b"\r\n"):
        rec = raw.lstrip(b"\x00")
        got = parse_se_prize(rec)
        if got:
            yield got


def build_prize_index(raw_dir: str | Path, pattern: str = "SESW*.jvd") -> dict:
    """raw ディレクトリを走査して賞金索引を作る。{race_key: {馬番: {...}}}"""
    index: dict[str, dict] = {}
    for p in sorted(Path(raw_dir).glob(pattern)):
        for key, hn, vals in iter_se_prizes(p):
            index.setdefault(key, {})[hn] = vals      # 差分更新は後勝ち
    return index


def save_prize_index(index: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return out


def load_prize_index(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def prize_of(index: dict, race_key: str, horse_num: str) -> float | None:
    """本賞金 (円)。索引に無ければ None (誠実に劣化)。"""
    v = (index.get(race_key) or {}).get(horse_num)
    return None if not v else v.get("honsho_yen")
