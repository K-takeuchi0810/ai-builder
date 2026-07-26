"""きょうの順位 (リーダーボード) — UI指示書 §5。

参加者が作った各マイAIについて、**当日の確定済みレース**での成績を集計する。

## 順位規則 (指示書で確定)

    ① ◎的中数 (win_hits)
    ② 人気を出し抜いた的中数 (upset_hits) = ◎が1番人気と違い、かつ的中した数
    ③ ◎複勝率 (show_rate)
    ④ それでも同点なら **同順位** で表示する

## 1番人気ベースライン

`is_baseline: true` で返す。UI は背景色を変え、**順位数字を表示しない**
(参加者と競わせないため)。順位計算からも除外する。

回収率は集計しない (設計書 §2: 回収率をメイン指標にしない。ROI キーを持たない)。
"""

from __future__ import annotations

from . import configs as cf
from . import model
from . import predict_service as svc


def _blank() -> dict:
    return {"races": 0, "win_hits": 0, "show_hits": 0, "upset_hits": 0}


def _tally(acc: dict, pick: str | None, favorite: str | None, order: dict) -> None:
    """1 レース分を加算する。pick=◎の馬番。"""
    if pick is None:
        return
    o = order.get(pick)
    if not isinstance(o, int) or o <= 0:
        return                      # 結果未確定 (or 取消) は集計対象外
    acc["races"] += 1
    won = o == 1
    if won:
        acc["win_hits"] += 1
        if favorite is not None and pick != favorite:
            acc["upset_hits"] += 1   # 人気を出し抜いた的中
    if 1 <= o <= 3:
        acc["show_hits"] += 1


def _entry(name: str, acc: dict, *, config_id: str | None = None,
           is_baseline: bool = False) -> dict:
    n = acc["races"]
    return {
        "config_id": config_id,
        "name": name,
        "races": n,
        "win_hits": acc["win_hits"],
        "upset_hits": acc["upset_hits"],
        "show_rate": round(acc["show_hits"] / n, 4) if n else None,
        "is_baseline": is_baseline,
    }


def _sort_key(e: dict):
    # ①◎的中数 ②出し抜き数 ③複勝率 の降順
    return (-e["win_hits"], -e["upset_hits"], -(e["show_rate"] or 0.0))


def _assign_ranks(entries: list[dict]) -> None:
    """同点は同順位。ベースラインには順位を付けない (rank=None)。"""
    rank = 0
    prev_key = None
    seen = 0
    for e in entries:
        if e["is_baseline"]:
            e["rank"] = None
            continue
        seen += 1
        key = _sort_key(e)
        if key != prev_key:
            rank = seen
            prev_key = key
        e["rank"] = rank


def build_leaderboard(daily: dict, preset: dict, *,
                      configs: list[dict] | None = None,
                      as_of: str | None = None) -> dict:
    """当日の確定レースから順位表を作る。

    daily: matrix_daily の当日行列。preset: プリセット重み。
    configs: [{"id","name","config"}] (省略時は保存済み全設定を使う)。
    """
    races = [r for r in daily.get("races", [])
             if any(h.get("order") == 1 for h in r.get("horses", []))]

    if configs is None:
        configs = []
        store = cf._load_store()
        for cid, entry in store.items():
            got = cf.get_config(cid)
            if got:
                configs.append({"id": cid, "name": entry.get("name") or cid,
                                "config": got["config"]})

    accs = {c["id"]: _blank() for c in configs}
    base = _blank()
    weights_by_id = {c["id"]: cf.column_weights(c["config"], preset.get("weights") or {})
                     for c in configs}
    columns_by_id = {c["id"]: cf.selected_columns(c["config"]) for c in configs}

    for r in races:
        rows = {h["num"]: h["x"] for h in r["horses"]}
        order = {h["num"]: h.get("order") for h in r["horses"]}
        favorite = next((h["num"] for h in r["horses"] if h.get("pop") == 1), None)
        _tally(base, favorite, favorite, order)      # ベースライン = 1番人気を◎とする
        for c in configs:
            ranked = model.score_columns_detailed(
                rows, columns_by_id[c["id"]], weights_by_id[c["id"]])["ranked"]
            pick = ranked[0][0] if ranked else None
            _tally(accs[c["id"]], pick, favorite, order)

    entries = [_entry(c["name"], accs[c["id"]], config_id=c["id"]) for c in configs]
    entries.append(_entry("1番人気AI", base, is_baseline=True))
    entries.sort(key=_sort_key)
    _assign_ranks(entries)

    return {
        "as_of": as_of or daily.get("date"),
        "date": daily.get("date"),
        "n_races_finished": len(races),
        "ranking_rule": "同点のときは出し抜き数→複勝率の順で決まります",
        "entries": entries,
        "marks": svc.MARKS,
    }
