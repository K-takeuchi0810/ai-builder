"""参加者の設定 (マイAI) の正規化・ハッシュ・保存、および列と重みへの変換。

設計書 §8 の config スキーマ:

    {
      "name": "参加者AI 1",
      "step1": ["popularity", "jockey_win_rate", ...],                 # 単項目
      "step2": [{"metric": "agg_avg_finish", "match": ["distance"], "lookback": 3}, ...]
    }

## 複数選択の合成 (設計書 §4「基底列の単純平均」の実装)

同じ集計対象 (metric) について複数セルを選んだ場合、**基底列の平均**で合成する。
実装は「各セルの重み ÷ そのmetricで選ばれたセル数」とする。プリセット重みが等しければ
数学的に単純平均と同値で、かつ **セルごとの寄与を個別に表示できる**ため、
設計書 §3.1 の「タイム指数(同距離・直近3走平均) +2.1」というセル単位の説明が可能になる。

重み自体はユーザーが決めない (方式B: プリセット重み)。ユーザーは**列を選ぶだけ**。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config as cfgmod
from . import matrix as mx
from . import model
from . import specs as sp


def excluded_in_config(cfg: dict) -> list[str]:
    """設定に含まれている **参加者AIでは使えない項目** (判断A の除外対象)。

    v0.3 より前に保存された設定には「人気(市場)」が入っている。黙って落とすと
    参加者は「選んだのに効いていない」ことに気づけないので、警告に載せるために
    何が落ちたかを返す。
    """
    keys = set(cfg.get("step1") or [])
    keys |= {c.get("metric") or c.get("key") for c in (cfg.get("step2") or [])}
    return sorted(k for k in keys if k in sp.PARTICIPANT_EXCLUDED_KEYS)


def normalize_config(cfg: dict) -> dict:
    """設定を正規化 (重複除去・順序固定)。同一設定が同一ハッシュになるようにする。

    判断A の除外対象 (「人気(市場)」) はここで落とす。**唯一の入口**にすることで、
    参加者経路 (選択列・重み・寄与・バックテスト・順位表) のどこにも混入しない。
    """
    step1 = sorted({k for k in (cfg.get("step1") or [])
                    if k in model.FEATURES and k not in sp.PARTICIPANT_EXCLUDED_KEYS})
    seen = set()
    step2 = []
    for cell in cfg.get("step2") or []:
        key = cell.get("metric") or cell.get("key")
        if key not in model.FEATURES or key in sp.PARTICIPANT_EXCLUDED_KEYS:
            continue
        match = sorted(cell.get("match") or [])
        lb = cell.get("lookback")
        lb = None if lb in (None, "", 0) else int(lb)
        sig = (key, tuple(match), lb)
        if sig in seen:
            continue
        seen.add(sig)
        step2.append({"metric": key, "match": match, "lookback": lb})
    step2.sort(key=lambda c: (c["metric"], tuple(c["match"]), -1 if c["lookback"] is None
                             else c["lookback"]))
    return {"name": (cfg.get("name") or "").strip(), "step1": step1, "step2": step2}


def config_hash(cfg: dict) -> str:
    """正規化した設定のハッシュ。名前は含めない (同一構成の成績を集約するため)。"""
    n = normalize_config(cfg)
    payload = json.dumps({"step1": n["step1"], "step2": n["step2"]},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def selected_columns(cfg: dict) -> list[dict]:
    """設定 → 使用する基底列 (matrix._columns と同じ形)。"""
    n = normalize_config(cfg)
    specs = [{"key": k} for k in n["step1"]]
    specs += [{"key": c["metric"], "match": c["match"], "lookback": c["lookback"]}
              for c in n["step2"]]
    return mx._columns(specs)


def column_weights(cfg: dict, preset_weights: dict[str, float]) -> dict[str, float]:
    """設定 → {列ID: 重み}。同じ metric で複数セル選択時は平均になるよう割る。"""
    n = normalize_config(cfg)
    out: dict[str, float] = {}
    for k in n["step1"]:
        cid = mx._col_id({"key": k})
        out[cid] = float(preset_weights.get(cid, 0.0))

    groups: dict[str, list[str]] = {}
    for c in n["step2"]:
        cid = mx._col_id({"key": c["metric"], "match": c["match"], "lookback": c["lookback"]})
        groups.setdefault(c["metric"], []).append(cid)
    for _metric, cids in groups.items():
        share = 1.0 / len(cids)              # 基底列の単純平均に相当
        for cid in cids:
            out[cid] = float(preset_weights.get(cid, 0.0)) * share
    return out


# ---------------------------------------------------------------------------
# 保存とバージョン管理 (設計書 §3.2 「マイAI v1 → v2」)
# ---------------------------------------------------------------------------
def _store_path() -> Path:
    return Path(cfgmod.PRESET_WEIGHTS_PATH).parent / "configs.json"


def _load_store() -> dict:
    p = _store_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_store(store: dict) -> Path:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def save_config(cfg: dict, config_id: str | None = None) -> dict:
    """設定を保存し、同一 id への保存は version を繰り上げる (マイAI v1 → v2)。"""
    n = normalize_config(cfg)
    cid = config_id or config_hash(n)[:8]
    store = _load_store()
    entry = store.get(cid) or {"id": cid, "versions": []}
    version = len(entry["versions"]) + 1
    entry["versions"].append({"version": version, "config": n, "hash": config_hash(n)})
    entry["name"] = n["name"] or entry.get("name") or f"参加者AI {cid}"
    entry["current_version"] = version
    store[cid] = entry
    _save_store(store)
    return {"id": cid, "version": version, "name": entry["name"], "config": n,
            "hash": config_hash(n)}


def get_config(config_id: str, version: int | None = None) -> dict | None:
    entry = _load_store().get(config_id)
    if not entry:
        return None
    if version is None:
        version = entry["current_version"]
    got = next((v for v in entry["versions"] if v["version"] == version), None)
    if not got:
        return None
    return {"id": config_id, "name": entry.get("name"), **got}


def config_history(config_id: str) -> dict | None:
    entry = _load_store().get(config_id)
    if not entry:
        return None
    return {"id": config_id, "name": entry.get("name"),
            "current_version": entry["current_version"],
            "versions": [{"version": v["version"], "hash": v["hash"],
                          "n_step1": len(v["config"]["step1"]),
                          "n_step2": len(v["config"]["step2"])}
                         for v in entry["versions"]]}
