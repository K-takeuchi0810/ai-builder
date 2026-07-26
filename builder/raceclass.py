"""生 JV-Data (RA レコード) から **競走条件コード** を復元する。

## なぜ必要か

`keiba.db` の `races` テーブルには競走条件コードが無い。あるのは

- `race_type_code` … **年齢条件** (サラ2歳 / サラ3歳 / サラ3歳以上 …)。クラスではない
- `grade_code` … 重賞グレード (G1/G2/G3/リステッド)。平場・条件戦では空

つまり「新馬 / 未勝利 / 1勝クラス / 2勝 / 3勝 / オープン」という**クラス**が
DB から取れない。UI ではレース名の無い平場でクラスを出したいので、
corner.py / prize.py と同じ方針で **生 RA を read-only で読んで復元する**。

## オフセットの特定と検証

DB が持つ `race_type_code + race_symbose_code + weight_type_code` を
バイト列として生 RA 内で検索し、その並びから前後の構造を確定した。

    610  重賞回次(4)
    614  グレードコード(1)
    615  変更前グレードコード(1)
    616  競走種別コード(2)      ← DB の race_type_code と一致
    618  競走記号コード(3)      ← DB の race_symbol_code と一致
    621  重量種別コード(1)      ← DB の weight_type_code と一致
    622  競走条件コード(3 × 5)  ← 2歳/3歳/4歳/5歳以上/最若年

クラスとして使うのは **最若年条件** (5番目)。2,078レースでの分布が
仕様どおりであることを確認した:

    703 未勝利   801 件 (最多。実際に未勝利は最も多い)
    005 1勝      425
    010 2勝      206   例: 胎内川特別
    999 オープン 184   例: **東京優駿 / ＮＨＫマイルカップ** ← 外部照合として決定的
    701 新馬     116
    016 3勝       90   例: 三条ステークス / 是政ステークス
    702 未出走    31
    004/009/014/015 … 1989〜1994 の旧条件 (400万下・900万下 等)

`keiba.db` には一切書き込まない (不変条件を維持)。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

RA_MIN_LEN = 640                  # 条件コードの終端 (637) を含む長さ
COND_START = 622                  # 0-based。競走条件コード (3 バイト × 5)
GRADE_POS = 614                   # グレードコード (1)
RACE_KEY_SLICE = slice(11, 27)    # 年4+月日4+場2+回2+日2+R2

# 競走条件コード → クラス名。現行コードのみ定義し、旧コード (1990年代の
# 400万下等) は素通しする — 参加者に出すのは当日のレースなので現行だけで足りる。
CONDITION_NAMES: dict[str, str] = {
    "701": "新馬",
    "702": "未出走",
    "703": "未勝利",
    "005": "1勝クラス",
    "010": "2勝クラス",
    "016": "3勝クラス",
    "999": "オープン",
}

# グレードコード → 表示名。**実データで確認できた分だけ** を定義する。
# keiba-yosou の web/codes.py は E を "L" としているが、実データでは
# E は「わらび賞」「胎内川特別」「羊ヶ丘特別」など重賞以外の特別戦に付き、
# リステッドの「札幌日経賞」には L が付いていた。混同すると平場に
# 「L」と出てしまうので、こちらの表を使う。
GRADE_NAMES: dict[str, str] = {
    "A": "G1", "B": "G2", "C": "G3", "L": "リステッド",
}


def parse_conditions(rec: bytes) -> dict:
    """RA レコード 1 件 → {"grade": コード, "conditions": [5個], "class_code": 最若年}。"""
    if len(rec) < RA_MIN_LEN:
        return {}
    raw = rec[COND_START:COND_START + 15].decode("ascii", "replace")
    parts = [raw[i:i + 3] for i in range(0, 15, 3)]
    return {
        "grade": rec[GRADE_POS:GRADE_POS + 1].decode("ascii", "replace").strip(),
        "conditions": parts,
        "class_code": parts[4].strip(),      # 最若年条件をクラスとして使う
    }


def race_class_label(class_code: str | None, grade: str | None) -> str | None:
    """クラス表示名。重賞グレードがあればそちらを優先する。

    G1/G2/G3/リステッドはクラスより強い情報なので前に出す。
    未知のコードは捏造せず None を返す (UI は条件行だけを出す)。
    """
    g = (grade or "").strip()
    if g in GRADE_NAMES:
        return GRADE_NAMES[g]
    return CONDITION_NAMES.get((class_code or "").strip())


# ---------------------------------------------------------------------------
# 索引の構築 (corner.py / prize.py と同じ方式)
# ---------------------------------------------------------------------------
def _raw_dir() -> Path:
    return Path(config.KEIBA_YOSOU_PATH) / "data" / "raw" / "RACE"


def index_path() -> Path:
    return Path(config.CORNER_INDEX_PATH).parent / "raceclass_index.json"


def build_index(rebuild: bool = False) -> dict:
    """生 RA を走査して {レースキー: {"class_code", "grade"}} の索引を作る。"""
    p = index_path()
    if p.exists() and not rebuild:
        return json.loads(p.read_text(encoding="utf-8"))

    out: dict[str, dict] = {}
    for f in sorted(_raw_dir().glob("RA*.jvd")):
        data = f.read_bytes()
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i:i + 2] == b"RA" and data[i + 2:i + 3].isdigit():
                rec = data[i:i + 1273]
                got = parse_conditions(rec)
                if got and got["class_code"]:
                    key = rec[RACE_KEY_SLICE].decode("ascii", "replace")
                    out[key] = {"class_code": got["class_code"], "grade": got["grade"]}
                i += 1273
            else:
                i += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return out


_CACHE: dict | None = None


def lookup(race: dict) -> dict:
    """レース (keiba.db の行) → {"class_code", "grade"}。無ければ空 dict。

    索引が無い期間は空で返し、UI は条件行だけを出す (誠実に劣化)。
    """
    global _CACHE
    if _CACHE is None:
        p = index_path()
        _CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    key = (f"{race.get('race_year')}{race.get('race_month_day')}"
           f"{race.get('track_code')}{race.get('kaiji')}"
           f"{race.get('nichiji')}{race.get('race_num')}")
    return _CACHE.get(key, {})
