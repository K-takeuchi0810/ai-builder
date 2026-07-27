"""設計書 §6/§7 の中核: 印の生成 (寄与分解つき) とバックテスト再生。

API 層 (builder/api.py) はここを呼ぶだけにして、ロジックをテスト可能に保つ。

- 印: スコア降順に ◎ ○ ▲ △ × (6頭目以降は無印)
- 自信度: 1位と2位のスコア差を、学習期間の分位点で3段階 (鉄板級/有力/混戦)
- 寄与分解とカバレッジを必ず同梱 (説明可能性が本体)
- **回収率はメイン指標にしない** (設計書 §2)。バックテストは的中率系を返す。
"""

from __future__ import annotations

from . import configs as cf
from . import config as cfgmod
from . import matrix as mx
from . import model
from . import presets as ps
from . import roi as _roi

MARKS = ["◎", "○", "▲", "△", "×"]


def _rows_from_race(race: dict) -> dict[str, dict]:
    return {h["num"]: h["x"] for h in race.get("horses", [])}


def predict_race(race: dict, user_config: dict, preset: dict) -> dict:
    """1 レースを参加者の設定で予想する。

    race: matrix_daily / matrix の 1 レース分。preset: presets.fit_presets の結果。
    """
    columns = cf.selected_columns(user_config)
    weights = cf.column_weights(user_config, preset.get("weights") or {})
    rows = _rows_from_race(race)
    if not rows:
        return {"race_id": race.get("race_id"), "marks": [], "error": "no_horses"}

    warnings = []
    # 判断A: v0.3 より前に保存された設定には「人気(市場)」が入っている。
    # normalize_config が落とすので印には影響しないが、黙って落とすと参加者は
    # 「選んだのに効いていない」ことに気づけないので必ず知らせる。
    dropped = cf.excluded_in_config(user_config)
    if dropped:
        warnings.append({
            "code": "excluded_columns_dropped",
            "message": "「人気(市場)」は予想に使わない項目になったため、この設定から外しました",
            "hint": "市場人気は基準の「1番人気AI」専用です。マイAIは選んだ項目だけで印を決めます。",
            "columns": [{"label": model.FEATURES[k].label} for k in dropped
                        if k in model.FEATURES],
        })

    # 選べる列が1つも無い場合。判断A で「人気(市場)」だけを選んでいた旧設定が
    # ここに落ちる (除外後に選択が空になる)。列が空だと全馬のスコアが 0 になり
    # 印は並び順そのままの無意味な値になるので、**必ず** 警告して止める。
    if not columns:
        warnings.append({
            "code": "no_columns_selected",
            "message": "予想に使える項目が選ばれていません (印は出せません)",
            "hint": "「マイAIをつくる」から項目を1つ以上選んでください。",
        })

    # プリセット重みが選択列をカバーしていないと全重み0 = 印が無意味になる。
    # 黙って順位を出すと参加者に嘘を見せるので、必ず警告として返す。
    if columns and not any(weights.get(c["id"]) for c in columns):
        warnings.append({
            "code": "no_preset_weights",
            "message": "選択された項目にプリセット重みがありません (印は無意味です)",
            "hint": "この列構成でプリセット重みを学習してください",
        })

    res = model.score_columns_detailed(rows, columns, weights)
    ranked = res["ranked"]
    used = sum(1 for c in res["columns"] if c["decision"] == "used")
    # **使える項目が0なら印を返さない。** 全馬スコア0だと ranked は入力順のままで、
    # それに ◎○▲△× を付けると「入力順を順位として提示する」ことになる。
    # UI 側のガードだけに頼らず、API がそもそも印を出さない (fail-closed)。
    suppress_marks = used == 0
    if columns and used == 0 and not warnings:
        warnings.append({
            "code": "all_columns_gated_out",
            "message": "選択された全項目がカバレッジ不足でこのレースでは使えません",
            "hint": "項目を増やすか、直近走の少ない馬が多いレースを避けてください",
        })
    _annotate_low_sample(res, preset, warnings)
    _warn_skipped_columns(columns, weights, res, warnings)
    _annotate_plain_values(res)
    # 自信度は **尺度不変な差** で判定する。生の差は選んだ項目数と重みの大きさに
    # 比例するので、425列で決めた閾値を数項目の設定に当てると常に「混戦」になる。
    gap = ps.normalized_gap([s for _n, s in ranked])
    raw_gap = (ranked[0][1] - ranked[1][1]) if len(ranked) >= 2 else None
    thresholds = preset.get("confidence_thresholds") or {}

    by_num = {h["num"]: h for h in race["horses"]}
    marks = []
    for i, (num, score) in enumerate([] if suppress_marks else ranked):
        h = by_num.get(num, {})
        cov = res["coverage"].get(num, {})
        contribs = res["contributions"].get(num, [])
        marks.append({
            "rank": i + 1,
            "mark": MARKS[i] if i < len(MARKS) else "",
            # 初心者にはこの一文が本文。サーバで生成して表示ロジックを複製させない
            "decisive": _decisive_sentence(
                contribs, MARKS[i] if i < len(MARKS) else "", i + 1, len(ranked)),
            "horse_num": num,
            "waku": h.get("waku"),        # DB 由来。UI は表示のみ (計算しない)
            "horse_name": h.get("name"),
            "score": round(score, 4),
            "popularity": h.get("pop"),
            "odds": h.get("odds"),
            # 出走情報 (行と whyカードで出す)。UI は表示のみ。
            "jockey": h.get("jockey"),
            "burden_weight": h.get("burden_weight"),
            "trainer": h.get("trainer"),
            "sex_age": h.get("sex_age"),
            "horse_weight": h.get("horse_weight"),
            "horse_weight_change": h.get("horse_weight_change"),
            "n_past_runs": h.get("n_past_runs"),
            # 過去走が少ない馬は印の直下で開示する (whyカードを開かなくても見える)
            "few_past_runs": (isinstance(h.get("n_past_runs"), int)
                              and h["n_past_runs"] < MIN_PAST_RUNS),
            "coverage": cov,
            "contributions": contribs,
        })
    return {
        "race_id": race.get("race_id"),
        "race_name": race.get("race_name"),
        "date": race.get("date"),
        "marks": marks,
        "columns": res["columns"],
        "confidence": _confidence(gap, raw_gap, thresholds, used, len(columns)),
        "weight_announced": race.get("weight_announced"),
        "config_hash": cf.config_hash(user_config),
        "warnings": warnings,
        "n_columns_used": used,
        # 予想画面を一覧レスポンスに依存させないためのヘッダ情報
        "race_num": race.get("race_num"),
        "start_time": race.get("start_time"),
        "odds_as_of": race.get("odds_as_of"),
        "odds_trusted": race.get("trusted"),
        "n_columns_selected": len(columns),
        # 発走済みなら着順を返す。印は発走前のレースにだけ意味があるので、
        # UI は終了レースでは印の代わりに結果を出す。
        "finished": any(h.get("order") == 1 for h in race["horses"]),
        "result": _result_top3(race),
        # 買い目 (印の並べ替え)。金額は扱わず、QR も生成しない。
        # スマッピー投票の QR データ形式は非公開なので、公式サイトへ手入力する
        # ための一覧として出す (builder/betslip.py)。
        "bet_slip": _bet_slip(marks),
    }


def _bet_slip(marks: list[dict]) -> list[dict]:
    """印から買い目を組む。印が無ければ空 (印を出さないレースでは買い目も出さない)。"""
    from . import betslip
    return betslip.build(marks) if marks else []


def _result_top3(race: dict) -> list[dict]:
    """確定していれば上位3頭 (着順・馬番・馬名)。未確定なら空。"""
    got = [h for h in race.get("horses", [])
           if isinstance(h.get("order"), int) and 1 <= h["order"] <= 3]
    got.sort(key=lambda h: h["order"])
    return [{"order": h["order"], "horse_num": h["num"], "horse_name": h.get("name")}
            for h in got]


MIN_PAST_RUNS = 3          # これ未満は「参照できた過去走が少ない」として開示する


def _decisive_sentence(contribs: list, mark: str, rank: int = 1,
                       n_runners: int | None = None) -> str | None:
    """決め手の一文。**順位の文脈から書き始める。**

    以前は寄与の向きだけで書いていたため、×印 (12頭中5番目) の馬に
    「評価を下げています」+ 下げた内訳しか出ず、「悪い馬になぜ印が付くのか」が
    説明されていなかった。印は絶対評価ではなく **他馬との相対順位** なので、
    下位の印では順位を先に述べる。
    """
    used = [c for c in contribs if c.get("available")]
    if not used:
        return None
    ranked = sorted(used, key=lambda c: abs(c.get("contribution") or 0.0), reverse=True)
    top = ranked[0]
    v = top.get("contribution") or 0.0
    if v == 0.0:
        return None
    label = top.get("label") or ""
    field = f"{n_runners}頭中" if n_runners else ""
    up = v > 0

    # 下位の印 (△×) で押し上げが無い場合は、順位から書く
    if mark in ("△", "×") and not up:
        return (f"{field}{rank}番目の評価です。"
                f"「{label}」は低めですが、他の馬より相対的に上でした。")

    head = (f"「{label}」が出走馬の中で高いことが決め手です。" if up
            else f"{field}{rank}番目の評価です。「{label}」が低く、評価を下げています。")

    tail = ""
    if len(ranked) >= 2:
        second = ranked[1]
        sv = second.get("contribution") or 0.0
        if sv != 0.0 and abs(sv) / abs(v) >= 0.8:
            # 2文目は1文目と向きが揃っているかで書き分ける。揃っていないのに
            # 「も」でつなぐと意味の通らない文になる。
            same = (sv > 0) == up
            lbl2 = second.get("label") or ""
            if same:
                verb = "も後押ししています" if up else "も評価を下げています"
                tail = f"「{lbl2}」{verb}。"
            else:
                tail = (f"ただし「{lbl2}」は評価を下げています。" if up
                        else f"ただし「{lbl2}」は評価を上げています。")
    return head + tail



_SKIP_REASON = {
    "skipped_low_coverage": "このレースでは値のある馬が少なく、使えませんでした",
    "skipped_no_variance": "全馬が同じ値で、差がつきませんでした",
    "no_weight": "この項目には学習済みの重みがありません",
}


def _warn_skipped_columns(columns: list, weights: dict, res: dict,
                          warnings: list) -> None:
    """**選んだのにこのレースで使えなかった項目**を名前と理由つきで開示する。

    ヘッダの「分析に使えた項目 1/3」だけでは、どの項目が落ちたのか分からない。
    寄与分解にも現れない (使われていないので寄与が無い) ため、黙っていると
    参加者は「選んだのに反映されていない」ことに気づけないまま印を見る。

    印そのものは残った項目で有効なので、開示のみの警告として扱う。
    """
    if not columns:
        return
    seen = {c["id"]: c for c in res["columns"]}
    skipped = []
    for c in columns:
        got = seen.get(c["id"])
        if got is None:
            # 重み0の項目は採点に入らないので res["columns"] に現れない
            if not weights.get(c["id"]):
                skipped.append((c.get("label", c["id"]), "no_weight"))
            continue
        if got["decision"] != "used":
            skipped.append((got.get("label", c["id"]), got["decision"]))
    if not skipped:
        return
    warnings.append({
        "code": "columns_skipped_in_race",
        "message": f"選んだ項目のうち {len(skipped)} 件は、このレースでは使えませんでした",
        "hint": "残りの項目で印を付けています。使えた項目が少ないほど確かさは下がります。",
        "columns": [{"label": lab, "reason": _SKIP_REASON.get(d, d)}
                    for lab, d in skipped[:8]],
        "n_columns": len(skipped),
    })


def _annotate_low_sample(res: dict, preset: dict, warnings: list) -> None:
    """学習サンプルが薄い項目に印を付け、**使われている場合だけ**警告する。

    コーナー通過順位は生 JV-Data の保存範囲が 2025年以降しかなく、ゲートを通った
    学習レースが中央値 53 件しかない (賞金系は 143 件)。これらの重みは 15,000 件で
    学習した項目と同じ確度ではない。黙って同じ見た目で出すと参加者が確度を
    誤解するため、寄与の各行に印を付け、警告に項目名と学習レース数を明示する。

    **学習窓を後ろに伸ばして件数を稼ぐことはしない** (直近データで学習して直近で
    評価すれば成績が良く見えるだけで、実際の予測力ではない)。件数が薄いという
    事実をそのまま開示する。
    """
    low = {c["column"]: c["races_passed_gate"]
           for c in (preset.get("low_sample_columns") or [])}
    if not low:
        return
    for c in res["columns"]:
        if c["id"] in low:
            c["low_sample"] = True
            c["train_races"] = low[c["id"]]
    for contribs in res["contributions"].values():
        for c in contribs:
            if c["id"] in low:
                c["low_sample"] = True
                c["train_races"] = low[c["id"]]

    thin = [c for c in res["columns"] if c.get("low_sample") and c["decision"] == "used"]
    if not thin:
        return
    thin.sort(key=lambda c: c["train_races"])
    warnings.append({
        "code": "low_sample_columns",
        "message": f"選んだ項目のうち {len(thin)} 件は、重みを決めた過去レースが少ないため"
                   f"確度が低めです (最少 {thin[0]['train_races']}レース)",
        "hint": "コーナー通過順位・獲得本賞金は過去データの保存範囲が短く、"
                "他の項目より根拠が薄くなります。印はそのまま出しています。",
        "columns": [{"label": c["label"], "train_races": c["train_races"]}
                    for c in thin[:8]],
        "n_columns": len(thin),
    })


MIN_RACES_FOR_RATE = 100      # これ未満の条件は数値を出さず「データ不足」にする

# 条件別内訳の区分。**事前固定の粗い二分・三分だけ**にする。
# 距離帯×馬場×競馬場のような細分化は F3-EDGE-001 で棄却済みの探索空間に
# 逆戻りするので作らない (指示 R3-c の凍結)。
DISTANCE_BANDS: tuple[tuple[str, str, int, int], ...] = (
    ("short", "短距離", 0, 1400),
    ("mile", "マイル", 1401, 1800),
    ("long", "中長距離", 1801, 99999),
)


def distance_band(distance) -> str | None:
    d = model._num(distance)
    if not d:
        return None
    for key, _label, lo, hi in DISTANCE_BANDS:
        if lo <= d <= hi:
            return key
    return None


def condition_key(seg: dict) -> tuple[str | None, str | None]:
    """(芝ダート, 距離帯)。どちらかが取れなければ None。"""
    return (seg or {}).get("surface"), distance_band((seg or {}).get("distance"))


def _blank():
    return {"races": 0, "win": 0, "show": 0, "in_marks": 0, "rank_corr_sum": 0.0,
            "rank_corr_n": 0}


def _spearman(pairs: list[tuple[int, int]]) -> float | None:
    """印順位と着順の Spearman 相関 (同順位は無視した簡易版)。"""
    n = len(pairs)
    if n < 2:
        return None
    d2 = sum((a - b) ** 2 for a, b in pairs)
    denom = n * (n * n - 1)
    return 1.0 - (6.0 * d2 / denom) if denom else None


def backtest(matrix: dict, user_config: dict, preset: dict, *,
             date_from: str | None = None, date_to: str = "99999999") -> dict:
    """設計書 §7 バックテスト再生。既定期間は学習に使っていない表示期間。

    返すのは的中率系 (◎単勝的中率 / ◎複勝率 / 印内的中率 / 順位相関) と
    1番人気ベースライン。回収率はメイン指標にしない。
    """
    date_from = date_from or cfgmod.DISPLAY_BACKTEST_FROM
    columns = cf.selected_columns(user_config)
    weights = cf.column_weights(user_config, preset.get("weights") or {})

    acc, base = _blank(), _blank()
    # 条件別の内訳 (芝ダート / 距離帯)。参加者が「このレースに向くAIか」を
    # 判断できる材料を出すため。**重みは条件別に分けない** (R3-c の凍結)。
    by_cond: dict[str, dict] = {}
    base_by_cond: dict[str, dict] = {}
    # 回収率は蓄積しないと意味を持たない。表示期間 (数千レース) で集計する。
    roi_picks: list = []
    base_roi_picks: list = []

    for race in matrix.get("races", []):
        if not (date_from <= race["date"] <= date_to):
            continue
        rows = _rows_from_race(race)
        if len(rows) < 2:
            continue
        order = {h["num"]: h.get("order") for h in race["horses"]}
        if not any(o == 1 for o in order.values()):
            continue                              # 結果未確定は除外

        ranked = model.score_columns_detailed(rows, columns, weights)["ranked"]
        picks = [num for num, _ in ranked]
        _tally(acc, picks, order)
        if picks:
            roi_picks.append((picks[0], race))

        surface, band = condition_key(race.get("seg"))
        keys = [k for k in (surface, band,
                            (f"{surface}:{band}" if surface and band else None)) if k]

        fav = next((h["num"] for h in race["horses"] if h.get("pop") == 1), None)
        fav_picks = None
        if fav:
            fav_order = sorted(race["horses"],
                               key=lambda h: (h.get("pop") is None, h.get("pop") or 99))
            fav_picks = [h["num"] for h in fav_order]
            _tally(base, fav_picks, order)
            base_roi_picks.append((fav_picks[0], race))

        for k in keys:
            _tally(by_cond.setdefault(k, _blank()), picks, order)
            if fav_picks:
                _tally(base_by_cond.setdefault(k, _blank()), fav_picks, order)

    warnings = []
    if acc["races"] == 0:
        # 0 レースで的中率 None を返すと「成績が悪い」と誤読されるので必ず警告する
        warnings.append({
            "code": "no_races_in_period",
            "message": f"{date_from}〜{date_to} に対象レースがありません",
            "hint": "期間を広げるか、その期間の行列を構築してください",
        })
    return {"period": [date_from, date_to],
            "note": "過去の的中率は将来の成績を保証しません",
            "config_hash": cf.config_hash(user_config),
            "warnings": warnings,
            "your_ai": _summarize(acc), "baseline_favorite": _summarize(base),
            "min_races_for_rate": MIN_RACES_FOR_RATE,
            "by_condition": _condition_report(by_cond, base_by_cond),
            # 回収率。点推定だけでは判断できないので roi.py が不確かさを添える
            "roi_stats": _roi.summarize(_roi.unit_returns(roi_picks)),
            "baseline_roi_stats": _roi.summarize(_roi.unit_returns(base_roi_picks)),
            "roi_note": _roi_note()}


def _tally(acc: dict, picks: list[str], order: dict) -> None:
    acc["races"] += 1
    top = picks[0]
    o_top = order.get(top)
    if o_top == 1:
        acc["win"] += 1
    if isinstance(o_top, int) and 1 <= o_top <= 3:
        acc["show"] += 1
    winner = next((n for n, o in order.items() if o == 1), None)
    if winner in picks[:len(MARKS)]:
        acc["in_marks"] += 1
    pairs = [(i + 1, order[n]) for i, n in enumerate(picks)
             if isinstance(order.get(n), int) and order[n] > 0]
    rc = _spearman(pairs)
    if rc is not None:
        acc["rank_corr_sum"] += rc
        acc["rank_corr_n"] += 1


def _summarize(a: dict) -> dict:
    n = a["races"]
    return {
        "races": n,
        "hit_rate_win": round(a["win"] / n, 4) if n else None,
        "hit_rate_show": round(a["show"] / n, 4) if n else None,
        "hit_rate_in_marks": round(a["in_marks"] / n, 4) if n else None,
        "rank_corr": (round(a["rank_corr_sum"] / a["rank_corr_n"], 4)
                      if a["rank_corr_n"] else None),
    }


def _annotate_plain_values(res: dict) -> None:
    """寄与の各行に **平易表現** を添える (生の z を画面に出さないため)。

    z は「レース内で標準偏差いくつ分か」なので、参加者には意味が伝わらない。
    labels.plain_level が単一の辞書で 5段 (かなり上/上/平均的/下/かなり下) に
    変換する。UI 側に同じ表を作らせない。

    実値そのもの (55.0kg 等) は特徴量ごとに単位が違い、ここでは持っていない
    (行列は z 化前の生値を持つが列IDごとの単位表が無い)。**捏造せず**
    相対位置の表現だけを返す。
    """
    from . import labels as lb
    for contribs in res["contributions"].values():
        for c in contribs:
            c["value_text"] = lb.plain_level(c.get("z")) if c.get("available") else None


def _condition_report(by_cond: dict, base_by_cond: dict) -> list[dict]:
    """条件別の内訳。**レース数が少ない条件は数値を出さない。**

    100レース未満の的中率は当たり外れの偶然に支配されるので、数字を出すと
    「この条件は得意」と誤読される。`enough=False` で返して UI に
    「データ不足」と書かせる。
    """
    from . import labels as lb
    out = []
    band_label = {k: lab for k, lab, _lo, _hi in DISTANCE_BANDS}
    for key in sorted(by_cond):
        acc = by_cond[key]
        if ":" in key:
            sk, bk = key.split(":", 1)
            label = f"{lb.value_label('surface', sk)}{band_label.get(bk, bk)}"
        elif key in band_label:
            label = band_label[key]
        else:
            label = lb.value_label("surface", key)
        out.append({
            "key": key, "label": label, "races": acc["races"],
            "enough": acc["races"] >= MIN_RACES_FOR_RATE,
            "your_ai": _summarize(acc),
            "baseline_favorite": _summarize(base_by_cond.get(key, _blank())),
        })
    return out


def _roi_note() -> str:
    """回収率の数値に必ず添える注記 (labels ではなく roi.py の定数から作る)。"""
    return ("回収率は当たり外れの偶然に大きく左右されます。"
            f"{_roi.MIN_RACES_FOR_ROI}レース未満は数値を出しません。"
            f"単勝の控除率は{int(_roi.TAKEOUT * 100)}%なので、"
            f"長期の回収率は約{int(_roi.LONG_RUN_CEILING * 100)}%が上限です。")


# 使えた項目がこの割合を下回ったら自信度を降格する
LOW_COVERAGE_RATIO = 0.5


def _confidence(gap, raw_gap, thresholds: dict, used: int, selected: int) -> dict:
    """自信度。**使えた項目が少ないときは降格する。**

    3項目のうち2項目が使えず実質1項目の単純ソートなのに「有力」と出していた。
    スコア差が大きく見えるのは項目が1つしかないからで、確かさの根拠にならない。
    降格したことと理由を返し、UI が併記する。
    """
    label = ps.confidence_label(gap, thresholds)
    ratio = (used / selected) if selected else 0.0
    downgraded = False
    reason = None
    if used > 0 and ratio < LOW_COVERAGE_RATIO and label in ("鉄板級", "有力"):
        # 1段下げる (鉄板級→有力→混戦)
        label = "有力" if label == "鉄板級" else "混戦"
        downgraded = True
        reason = (f"選んだ項目のうち使えたのは {used}/{selected} 件なので、"
                  f"自信度を1段下げています")
    return {
        "normalized_gap": None if gap is None else round(gap, 4),
        "score_gap": None if raw_gap is None else round(raw_gap, 4),
        "label": label,
        "downgraded": downgraded,
        "downgrade_reason": reason,
        "coverage_ratio": round(ratio, 4),
    }
