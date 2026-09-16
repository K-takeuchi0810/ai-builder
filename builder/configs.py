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
import threading
import time
from pathlib import Path

from . import config as cfgmod
from . import matrix as mx
from . import model

_STORE_LOCK = threading.RLock()
from . import specs as sp


def excluded_in_config(cfg: dict) -> list[str]:
    """設定に含まれている **参加者AIでは使えない項目** (判断A の除外対象)。

    v0.3 より前に保存された設定には「人気(市場)」が入っている。黙って落とすと
    参加者は「選んだのに効いていない」ことに気づけないので、警告に載せるために
    何が落ちたかを返す。
    """
    keys = set(cfg.get("step1") or [])
    keys |= {c.get("metric") or c.get("key") for c in (cfg.get("step2") or [])}
    return sorted(k for k in keys if k in sp.PARTICIPANT_UNAVAILABLE_KEYS)


def normalize_config(cfg: dict) -> dict:
    """設定を正規化 (重複除去・順序固定)。同一設定が同一ハッシュになるようにする。

    判断A の除外対象 (「人気(市場)」) はここで落とす。**唯一の入口**にすることで、
    参加者経路 (選択列・重み・寄与・バックテスト・順位表) のどこにも混入しない。
    """
    step1 = sorted({k for k in (cfg.get("step1") or [])
                    if k in model.FEATURES and k not in sp.PARTICIPANT_UNAVAILABLE_KEYS})
    seen = set()
    step2 = []
    for cell in cfg.get("step2") or []:
        key = cell.get("metric") or cell.get("key")
        if key not in model.FEATURES or key in sp.PARTICIPANT_UNAVAILABLE_KEYS:
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


def _applied_path() -> Path:
    return Path(cfgmod.PRESET_WEIGHTS_PATH).parent / "applied_configs.json"


def _load_store() -> dict:
    with _STORE_LOCK:
        p = _store_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_store(store: dict) -> Path:
    with _STORE_LOCK:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return p


def remember_applied(owner_id: str | None, date: str, race_id: str,
                     config_id: str) -> None:
    """その日に実際に予想表示へ使ったAIをサーバ側へ記録する。"""
    owner_key = owner_id or "local"
    with _STORE_LOCK:
        path = _applied_path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        per_owner = data.setdefault(owner_key, {})
        # 当日比較専用なので、所有者ごとに直近7日分だけ保持する。
        per_owner.setdefault(str(date), {})[str(race_id)] = str(config_id)
        for old_date in sorted(per_owner)[:-7]:
            del per_owner[old_date]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def applied_configs(owner_id: str | None, date: str) -> dict[str, str]:
    owner_key = owner_id or "local"
    with _STORE_LOCK:
        path = _applied_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    got = (data.get(owner_key) or {}).get(str(date)) or {}
    return {str(race_id): str(config_id) for race_id, config_id in got.items()}


def _owned(entry: dict, owner_id: str | None) -> bool:
    """owner_id=None は非共有の従来運用、それ以外は完全一致だけを許可する。"""
    return owner_id is None or entry.get("owner_id") == owner_id


def _unique_name(store: dict, requested: str, owner_id: str | None,
                 *, exclude_id: str | None = None) -> str:
    base = requested.strip() or "マイAI 1"
    used = {str(e.get("name") or "") for cid, e in store.items()
            if cid != exclude_id and _owned(e, owner_id) and not e.get("archived")}
    if base not in used:
        return base
    i = 2
    while f"{base} {i}" in used:
        i += 1
    return f"{base} {i}"


def save_config(cfg: dict, config_id: str | None = None,
                owner_id: str | None = None) -> dict:
    """設定を保存し、同一 id への保存は version を繰り上げる (マイAI v1 → v2)。"""
    n = normalize_config(cfg)
    if config_id:
        cid = config_id
    elif owner_id:
        cid = hashlib.sha256(f"{owner_id}:{config_hash(n)}".encode()).hexdigest()[:12]
    else:
        cid = config_hash(n)[:8]
    with _STORE_LOCK:
        store = _load_store()
        entry = store.get(cid)
        if entry and owner_id is not None and entry.get("owner_id") != owner_id:
            raise PermissionError("このマイAIは別の利用者が所有しています")
        entry = entry or {"id": cid, "versions": [], "owner_id": owner_id,
                          "archived": False, "created_at": int(time.time())}
        if not _owned(entry, owner_id):
            raise PermissionError("このマイAIは別の利用者が所有しています")
        if not entry.get("versions"):
            n["name"] = _unique_name(store, n["name"], owner_id, exclude_id=cid)
        version = len(entry["versions"]) + 1
        entry["versions"].append({"version": version, "config": n, "hash": config_hash(n)})
        entry["name"] = n["name"] or entry.get("name") or f"参加者AI {cid}"
        entry["current_version"] = version
        entry["updated_at"] = int(time.time())
        store[cid] = entry
        _save_store(store)
    return {"id": cid, "version": version, "name": entry["name"], "config": n,
            "hash": config_hash(n)}


# ---------------------------------------------------------------------------
# 選び直しの記録 (過学習を見えるようにする)
# ---------------------------------------------------------------------------
# バックテストの数字を見ながら項目を選び直すと、その数字は **選び直した回数の
# ぶんだけ楽観側に寄る**。182,594 候補を機械で探索して out-of-sample のエッジが
# 出なかったのと同じことが手作業でも起きる。
# 世代ごとの調整側スコアを残しておけば、「選ぶほど上がっていく」形そのものが
# 過学習の痕跡として画面に出せる。
SCORE_HISTORY_LIMIT = 50


def record_score(config_id: str, version: int, score: dict,
                 owner_id: str | None = None) -> dict | None:
    """その世代の調整側スコアを残す。同じ世代は上書きする (行列は伸びるため)。"""
    with _STORE_LOCK:
        store = _load_store()
        entry = store.get(config_id)
        if not entry or not _owned(entry, owner_id):
            return None
        history = [x for x in (entry.get("scores") or [])
                   if int(x.get("version") or 0) != int(version)]
        history.append({
            "version": int(version),
            "races": int(score.get("races") or 0),
            "win_rate": score.get("win_rate"),
            "show_rate": score.get("show_rate"),
            "at": int(time.time()),
        })
        history.sort(key=lambda x: x["version"])
        entry["scores"] = history[-SCORE_HISTORY_LIMIT:]
        store[config_id] = entry
        _save_store(store)
        return entry["scores"][-1]


def score_history(config_id: str, owner_id: str | None = None) -> list[dict]:
    entry = _load_store().get(config_id)
    if not entry or not _owned(entry, owner_id):
        return []
    return list(entry.get("scores") or [])


def holdout_state(config_id: str, owner_id: str | None = None) -> dict:
    """封印期間を開封したか。開封後の選び直しは封印の意味を失う。"""
    entry = _load_store().get(config_id)
    if not entry or not _owned(entry, owner_id):
        return {}
    return dict(entry.get("holdout_revealed") or {})


def reveal_holdout(config_id: str, version: int,
                   owner_id: str | None = None) -> dict | None:
    """封印期間を開けたことを記録する。**取り消せない。**

    一度見たら、その数字を見ながら選び直せてしまう。記録を残すことで
    「この設定の封印は v3 で開封済み」と画面に出し続けられる。
    """
    with _STORE_LOCK:
        store = _load_store()
        entry = store.get(config_id)
        if not entry or not _owned(entry, owner_id):
            return None
        got = entry.get("holdout_revealed")
        if not got:
            got = {"version": int(version), "at": int(time.time())}
            entry["holdout_revealed"] = got
            store[config_id] = entry
            _save_store(store)
        return dict(got)


def get_config(config_id: str, version: int | None = None,
               owner_id: str | None = None) -> dict | None:
    entry = _load_store().get(config_id)
    if not entry:
        return None
    if not _owned(entry, owner_id):
        return None
    if version is None:
        version = entry["current_version"]
    got = next((v for v in entry["versions"] if v["version"] == version), None)
    if not got:
        return None
    return {"id": config_id, "name": entry.get("name"), **got}


def config_history(config_id: str, owner_id: str | None = None) -> dict | None:
    entry = _load_store().get(config_id)
    if not entry:
        return None
    if not _owned(entry, owner_id):
        return None
    return {"id": config_id, "name": entry.get("name"),
            "current_version": entry["current_version"],
            "versions": [{"version": v["version"], "hash": v["hash"],
                          "n_step1": len(v["config"]["step1"]),
                          "n_step2": len(v["config"]["step2"])}
                         for v in entry["versions"]]}


def list_configs(owner_id: str | None = None, *, include_archived: bool = False) -> list[dict]:
    """保存済みマイAIの一覧 (新しいものが先)。

    複数のマイAIを作ってレースごとに使い分けるための一覧。設定本体は返さず、
    選択に必要な情報だけを返す (画面の一覧が重くならないように)。
    """
    store = _load_store()
    out = []
    for cid, entry in store.items():
        if not _owned(entry, owner_id):
            continue
        if entry.get("archived") and not include_archived:
            continue
        cur = get_config(cid, owner_id=owner_id)
        if not cur:
            continue
        n = cur["config"]
        out.append({
            "id": cid,
            "name": entry.get("name") or cid,
            "version": cur["version"],
            "n_step1": len(n["step1"]),
            "n_step2": len(n["step2"]),
            "n_items": len(n["step1"]) + len(n["step2"]),
            "hash": cur["hash"],
            "archived": bool(entry.get("archived")),
            "updated_at": int(entry.get("updated_at") or entry.get("created_at") or 0),
            # 何回作り直したか。数字が良くなるまで選び直した痕跡になる
            "generations": len(entry.get("versions") or []),
            "holdout_revealed": dict(entry.get("holdout_revealed") or {}),
        })
    out.sort(key=lambda e: (e["archived"], -e["updated_at"], e["name"]))
    return out


def rename_config(config_id: str, name: str,
                  owner_id: str | None = None) -> dict | None:
    """名称だけを変更する (設定内容とバージョンは変えない)。"""
    with _STORE_LOCK:
        store = _load_store()
        entry = store.get(config_id)
        if not entry or not _owned(entry, owner_id):
            return None
        requested = (name or "").strip() or entry.get("name") or config_id
        entry["name"] = _unique_name(store, requested, owner_id, exclude_id=config_id)
        store[config_id] = entry
        _save_store(store)
    return {"id": config_id, "name": entry["name"]}


def archive_config(config_id: str, archived: bool = True,
                   owner_id: str | None = None) -> dict | None:
    """設定を復元可能な状態で一覧・集計から外す。"""
    with _STORE_LOCK:
        store = _load_store()
        entry = store.get(config_id)
        if not entry or not _owned(entry, owner_id):
            return None
        entry["archived"] = bool(archived)
        if not entry["archived"]:
            entry["name"] = _unique_name(
                store, entry.get("name") or config_id, owner_id, exclude_id=config_id)
        entry["updated_at"] = int(time.time())
        store[config_id] = entry
        _save_store(store)
    return {"id": config_id, "name": entry.get("name") or config_id,
            "archived": bool(entry["archived"])}


def migrate_legacy_configs(owner_id: str) -> dict:
    """所有者の無い旧設定を管理者へ移し、最も更新された1件以外を保管する。"""
    if not owner_id:
        raise ValueError("owner_id is required")
    with _STORE_LOCK:
        store = _load_store()
        legacy = [(cid, e) for cid, e in store.items() if not e.get("owner_id")]
        if not legacy:
            return {"migrated": 0, "archived": 0, "active_id": None}
        active_id, _active = max(
            legacy, key=lambda pair: (int(pair[1].get("current_version") or 0),
                                      len(pair[1].get("versions") or []))
        )
        archived_count = 0
        archived_index = 1
        for cid, entry in legacy:
            entry["owner_id"] = owner_id
            entry["updated_at"] = int(time.time())
            if cid == active_id:
                entry["archived"] = False
                if (entry.get("name") or "").startswith("参加者AI"):
                    entry["name"] = "マイAI 1"
            else:
                entry["archived"] = True
                entry["name"] = f"以前のマイAI {archived_index}"
                archived_index += 1
                archived_count += 1
            store[cid] = entry
        _save_store(store)
    return {"migrated": len(legacy), "archived": archived_count,
            "active_id": active_id}


def duplicate_config(config_id: str, name: str | None = None,
                     owner_id: str | None = None) -> dict | None:
    """複製して別のマイAIにする (編集の出発点にするため)。

    複製は **別の id** を持つ。同じ設定内容なら config_hash は同じになるが、
    id は名前と履歴を分けるための識別子なので新しく振る。
    """
    got = get_config(config_id, owner_id=owner_id)
    if not got:
        return None
    cfg = dict(got["config"])
    cfg["name"] = (name or f"{got.get('name') or config_id} のコピー").strip()
    store = _load_store()
    base = config_hash(cfg)[:8]
    cid = base
    i = 2
    while cid in store:                     # 同一内容の複製でも id を分ける
        cid = f"{base}-{i}"
        i += 1
    return save_config(cfg, config_id=cid, owner_id=owner_id)
