"""設計書 §8 の API (標準ライブラリのみ、外部依存なし)。

    GET  /api/races/today[?date=YYYYMMDD]   当日レース一覧 + 分析可否 (馬体重待ち/ゲート未通過)
    GET  /api/features                      選べる項目 (STEP1 / STEP2 の選択肢)
    POST /api/predict                       {race_id, config} → 印 + 寄与分解 + カバレッジ
    POST /api/backtest                      {config, period?} → 的中率系 + 人気ベースライン
    POST /api/configs                       設定の保存 (マイAI v1 → v2)
    GET  /api/configs/{id}                  設定の取得
    GET  /api/configs/{id}/history          成績推移用のバージョン履歴

起動 (通常は serve.bat から。システムの python では numpy が無く動かない):
    serve.bat
    serve.bat --date 20260801 --port 8780

**ブラウザで開くのは `http://127.0.0.1:8780/` (UI)。**
`/api/...` は JSON を返すエンドポイントで、画面ではない。

当日データは matrix_daily のキャッシュを使う (無ければ起動時に構築)。
プリセット重みは presets の保存済みファイルを読む (無ければ全重み0で動くが印は無意味)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config as cfgmod
from . import configs as cf
from . import matrix as mxmod
from . import matrix_daily as md
from . import model
from . import predict_service as svc
from . import presets as ps
from . import specs as sp

logger = logging.getLogger("builder.api")

_STATE: dict = {"date": None, "daily": {}, "preset": {}, "specs": [], "preview": False}


def _as_upcoming(obj: dict) -> dict:
    """検証モード: 確定済みのレースを「発走前」として見せる。

    過去日を開くと全レースが終了扱いになり、印の画面をまったく確認できない
    (開催日の発走前という短い時間帯しか触れない)。印は PIT を守って
    「そのレース以前の過去走」だけから作られているので、発走前に出したはずの
    印そのものではある — ただし結果を知っている状態で見るので、
    **常時バナーで検証モードだと明示する** (`version_info()["preview"]`)。
    """
    out = dict(obj)
    out["finished"] = False
    out["result"] = []
    return out

# UI の静的ファイル (素の HTML/CSS/JS)。外部依存ゼロ・1プロセス起動のため
# フレームワークやビルドツールは使わず、ここから直接配信する (UI指示書 §0)。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_SRC_DIR = Path(__file__).resolve().parent


def _fingerprint(paths) -> str:
    h = hashlib.sha1()
    for p in sorted(paths):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _py_fingerprint() -> str:
    """サーバ側コード (builder/*.py) の指紋。**プロセス起動時に固定される**。"""
    return _fingerprint(_SRC_DIR.glob("*.py"))


def _web_fingerprint() -> str:
    """web/ の指紋。リクエストごとにディスクから読むのでリロードで即反映される。"""
    return _fingerprint(WEB_DIR.glob("*"))


# 起動時の指紋。以降ディスクが変わってもこの値は変わらない。
_BOOT_PY = _py_fingerprint()
_BOOT_WEB = _web_fingerprint()
_BOOT_TIME = time.strftime("%Y-%m-%d %H:%M:%S")


def _built_dates() -> list[str]:
    """当日行列が構築済みの日付 (昇順)。レース0件のときの案内に使う。"""
    import re
    d = Path(cfgmod.CORNER_INDEX_PATH).parent / "daily"
    if not d.exists():
        return []
    ver = f"daily_v{md.DAILY_VERSION}_"
    return sorted({m.group(1) for p in d.glob(f"{ver}*.json")
                   if (m := re.search(r"_(\d{8})_", p.name))})


def version_info() -> dict:
    """起動時の指紋と現在のディスクを比べ、**再起動が必要か**を返す。

    「完了報告済みの修正が実機に出ていない」ときに、原因が
    (a) 古いプロセスが動いている (b) 実装されていない のどちらか分からず
    切り分けに時間を取られたため、機械的に判別できるようにする。

    再起動が必要なのは **builder/*.py が変わったときだけ**。web/ は
    リクエストごとに読み直すのでリロードで反映される — ここを混ぜて警告すると
    web を直すたびに「再起動してください」と嘘の指示を出すことになる。
    """
    py, web = _py_fingerprint(), _web_fingerprint()
    stale = py != _BOOT_PY
    web_changed = web != _BOOT_WEB
    if stale:
        msg = ("サーバ側のコードが起動時より新しくなっています。"
               "反映するには再起動してください (Ctrl+C → serve.bat)")
    elif web_changed:
        msg = "画面ファイルが更新されています (リロードで反映されます)"
    else:
        msg = "最新のコードで動作しています"
    return {
        "boot_fingerprint": _BOOT_PY,
        "disk_fingerprint": py,
        "web_fingerprint": web,
        "web_changed": web_changed,
        "stale": stale,
        "started_at": _BOOT_TIME,
        "message": msg,
        "date": _STATE.get("date"),
        "preview": bool(_STATE.get("preview")),
        "preview_message": ("検証モード: 確定済みのレースを発走前として表示しています"
                            "(結果は既に出ています)"),
    }


def _serve_static(handler: BaseHTTPRequestHandler, rel: str) -> None:
    """web/ 配下を配信する。パストラバーサルは拒否する。"""
    rel = unquote(rel).lstrip("/")
    if not rel or rel.endswith("/"):
        rel = "index.html"
    target = (WEB_DIR / rel).resolve()
    try:
        target.relative_to(WEB_DIR.resolve())      # web/ の外を指していないか
    except ValueError:
        return _json(handler, {"error": "forbidden"}, 403)
    if not target.is_file():
        return _json(handler, {"error": "not_found", "path": rel}, 404)

    ctype, _enc = mimetypes.guess_type(str(target))
    if target.suffix in (".html", ".css", ".js", ".json", ".svg"):
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                 ".json": "application/json", ".svg": "image/svg+xml"}[target.suffix]
        ctype += "; charset=utf-8"
    body = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")   # デモ中の差し替えを即反映
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler: BaseHTTPRequestHandler, obj, status: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def feature_catalog() -> dict:
    """UI が出す選択肢 (設計書 §4)。STEP2 は 9項目 × 条件4 × 期間11。"""
    from . import labels as lbl
    from . import presets as psmod
    # 学習サンプルが薄い項目を **選ぶ前に** 知らせる (事前マーク)。
    # 予想画面の事後警告と合わせて二段で開示する。
    preset = _STATE.get("preset") or psmod.load_presets()
    thin = {c["column"]: c["races_passed_gate"]
            for c in (preset.get("low_sample_columns") or [])}

    def _thin_for(key: str) -> dict:
        """その集計対象のセル群がどれくらい薄いか (最小の学習レース数)。"""
        ns = [n for cid, n in thin.items() if cid.split("|")[0] == key]
        return {"low_sample": True, "min_train_races": min(ns)} if ns else {}

    # 判断A: 「人気(市場)」は選択肢に出さない (設計書 v0.3 §2)
    step1 = [{"key": s["key"], "label": lbl.step1_label(s["key"]),
              "group": lbl.step1_group(s["key"]),
              "term": lbl.glossary_key(s["key"]),
              "category": model.FEATURES[s["key"]].category,
              **_thin_for(s["key"])}
             for s in sp.maib_participant_step1_specs()]
    # STEP2 の集計対象名は「(可変集計)」を外した素の名前 (セルは別の軸で選ばせる)
    _STEP2_TERM = {"agg_time_index": "time_index", "agg_prize": "prize",
                   "agg_margin": "margin", "agg_corner_first": "corner",
                   "agg_corner_last": "last_corner",
                   "agg_gain_first_to_last": "corner",
                   "agg_gain_first_to_finish": "corner",
                   "agg_gain_last_to_finish": "corner"}
    step2 = [{"metric": k, "label": model.FEATURES[k].label.replace("(可変集計)", ""),
              "term": _STEP2_TERM.get(k), **_thin_for(k)}
             for k in sp.MAIB_STEP2_METRICS if k in model.FEATURES]
    return {
        "step1": step1,
        "step1_groups": [{"key": g, "label": lab, "desc": d}
                         for g, lab, d in lbl.STEP1_GROUPS],
        # 初心者の空白画面問題への最小の答え: 迷ったら押せる一式
        "starter_preset": {
            "label": "まよったら",
            "desc": "実績・騎手・調子の3項目から始めます。あとから変えられます。",
            "step1": [k for k in ("fit_course", "jockey_recent_30d_top3_rate",
                                  "recent_trend_delta")
                      if k in model.FEATURES],
            "step2": [],
        },
        "glossary": lbl.glossary(),
        # 成績比較の並び順。board が空でも読めるよう選択肢と同じ経路で供給する
        "ranking_rule": lbl.RANKING_RULE,
        # 「過去走が少ない」の閾値。UI に数値を持たせない
        "min_past_runs": svc.MIN_PAST_RUNS,
        "marks": svc.MARKS,
        "mark_legend": [{"mark": m, "term": lbl.GLOSSARY[k]["term"],
                         "desc": lbl.GLOSSARY[k]["desc"]}
                        for m, k in zip(svc.MARKS + [""],
                                        ["honmei", "taikou", "tanana", "renka",
                                         "chuui", "mujirushi"])],
        "step2_metrics": step2,
        # 選択肢のラベルも labels.py を参照する (列ラベルと語彙をずらさない)
        "step2_matches": [{"value": m, "label": lbl.match_label(m)}
                          for m in sp.MAIB_MATCHES],
        "step2_lookbacks": [{"value": lb, "label": lbl.lookback_label(lb)}
                            for lb in sp.MAIB_LOOKBACKS],
        "n_base_columns": len(sp.maib_step2_specs()),
        "notes": ["重みは事前学習済み (参加者は項目を選ぶだけ)",
                  "回収率はメイン指標ではありません",
                  "賞金は「獲得本賞金」(付加賞・褒賞金を含まない)"],
    }


def handle_predict(payload: dict) -> tuple[dict, int]:
    race_id = payload.get("race_id")
    # config_id 指定にも対応する。**未知の id は 404**。
    # 空の config に落として続行すると、選択列0 = 全馬スコア0 のまま
    # 「◎」を返してしまう (入力順を順位として提示することになる)。
    cfg_id = payload.get("config_id")
    if cfg_id and not payload.get("config"):
        got = cf.get_config(str(cfg_id))
        if not got:
            return {"error": "config_not_found", "config_id": cfg_id}, 404
        user_cfg = got["config"]
    else:
        user_cfg = payload.get("config") or {}
    race = md.find_race(_STATE["daily"], race_id) if race_id else None
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    got = svc.predict_race(race, user_cfg, _STATE["preset"])
    # そのレースの条件でのマイAI成績を1行分だけ添える。
    # 「このレースに向くAIか」を判断する材料。重みは条件別に分けない (R3-c 凍結)。
    got["condition_record"] = _condition_record(race, user_cfg)
    if _STATE.get("preview"):
        got = _as_upcoming(got)
    # 起動時に検出したプリセットの問題は **どの種類でも** 必ず応答に載せる。
    # コードを1つだけ許可すると、新しい種類 (指紋なし等) が黙って素通りする。
    problem = _STATE.get("preset_problem")
    if problem:
        got.setdefault("warnings", []).append(problem)
    return got, 200


def _condition_record(race: dict, user_cfg: dict) -> dict | None:
    """このレースの条件 (芝ダート × 距離帯) でのマイAI成績。

    バックテスト用行列が読み込まれていないときは None (捏造しない)。
    レース数が閾値未満なら数値を出さず enough=False で返す。
    """
    src = _STATE.get("backtest_matrix")
    if not src or not src.get("races"):
        return None
    surface, band = svc.condition_key(race.get("seg"))
    if not surface or not band:
        return None
    key = f"{surface}:{band}"
    cache = _STATE.setdefault("_cond_cache", {})
    ck = (cf.config_hash(user_cfg), key)
    if ck in cache:
        return cache[ck]
    bt = svc.backtest(src, user_cfg, _STATE["preset"])
    got = next((c for c in bt["by_condition"] if c["key"] == key), None)
    cache[ck] = got
    return got


def handle_leaderboard(applied: dict | None = None) -> tuple[dict, int]:
    """本日の成績比較。当日の確定レースのみ集計。

    applied: {race_id: config_id}。レースごとに適用AIを切り替えた場合、
    その対応を渡すと「実際に使ったAI」に紐づけて集計する。
    """
    from . import leaderboard as lb
    daily = _STATE["daily"]
    if not daily.get("races"):
        return {"error": "daily_matrix_not_built", "date": _STATE["date"]}, 409
    return lb.build_leaderboard(daily, _STATE["preset"], applied=applied), 200


def handle_backtest(payload: dict) -> tuple[dict, int]:
    user_cfg = payload.get("config") or {}
    period = payload.get("period") or {}
    src = _STATE.get("backtest_matrix") or _STATE["daily"]
    if not src.get("races"):
        return {"error": "no_backtest_data",
                "hint": "起動時に --backtest-from/--backtest-to を指定してください"}, 409
    return svc.backtest(src, user_cfg, _STATE["preset"],
                        date_from=period.get("from"),
                        date_to=period.get("to", "99999999")), 200


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/")

        if path == "/api/races/today":
            date = q.get("date") or _STATE["date"]
            daily = (_STATE["daily"] if date == _STATE["date"]
                     else md.load_daily(date, _STATE["specs"]))
            if not daily:
                return _json(self, {"error": "daily_matrix_not_built", "date": date}, 409)
            races = md.today_status(daily)
            if _STATE.get("preview"):
                races = [_as_upcoming(r) for r in races]
            return _json(self, {"date": date, "races": races,
                                "preview": bool(_STATE.get("preview"))})

        if path == "/api/features":
            return _json(self, feature_catalog())

        if path == "/api/version":
            return _json(self, version_info())

        if path == "/api/leaderboard":
            return _json(self, *handle_leaderboard())


        if path == "/api/configs":
            return _json(self, {"configs": cf.list_configs()})

        if path.startswith("/api/configs/"):
            rest = path[len("/api/configs/"):]
            if rest.endswith("/history"):
                got = cf.config_history(rest[: -len("/history")])
                return _json(self, got or {"error": "not_found"}, 200 if got else 404)
            got = cf.get_config(rest)
            return _json(self, got or {"error": "not_found"}, 200 if got else 404)

        # UI (素の HTML/CSS/JS)。`/` と `/web/*` を web/ から配信する。
        if path in ("", "/"):
            return _serve_static(self, "index.html")
        if path.startswith("/web/"):
            return _serve_static(self, path[len("/web/"):])

        return _json(self, {"error": "not_found", "path": path}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        payload = self._body()
        if path == "/api/predict":
            return _json(self, *handle_predict(payload))
        if path == "/api/backtest":
            return _json(self, *handle_backtest(payload))
        if path == "/api/leaderboard":
            # 適用AIの対応を渡せる POST 版 (GET は全AI×全レース)
            return _json(self, *handle_leaderboard(payload.get("applied") or None))
        if path == "/api/configs":
            # 既存IDを指定すればその版を繰り上げる (複数マイAIの編集用)
            saved = cf.save_config(payload.get("config") or payload,
                                   config_id=payload.get("config_id"))
            return _json(self, saved, 201)
        if path == "/api/configs/rename":
            got = cf.rename_config(str(payload.get("config_id") or ""),
                                   payload.get("name") or "")
            return _json(self, got or {"error": "config_not_found"}, 200 if got else 404)
        if path == "/api/configs/duplicate":
            got = cf.duplicate_config(str(payload.get("config_id") or ""),
                                      payload.get("name"))
            return _json(self, got or {"error": "config_not_found"}, 201 if got else 404)
        return _json(self, {"error": "not_found", "path": path}, 404)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="MAIBuilder API (read-only)")
    ap.add_argument("--date", default=None,
                    help="当日日付 YYYYMMDD (既定: 今日)")
    ap.add_argument("--build", action="store_true", help="当日行列を無ければ構築する")
    ap.add_argument("--require-confirmed", action="store_true",
                    help="確定済みレースのみ (過去日で試すとき)")
    ap.add_argument("--preview", action="store_true",
                    help="確定済みレースを発走前として表示する (過去日で印の画面を"
                         "確認するための検証モード。画面に常時バナーが出る)")
    # 既定で表示期間 (学習未使用) を入れる。指定しないと「これまでの成績」カードが
    # 出ないのに理由が分からない、という迷い方をするため。
    ap.add_argument("--backtest-from", default=cfgmod.DISPLAY_BACKTEST_FROM,
                    help="バックテスト用行列の開始日 (既定: 表示期間の開始)")
    ap.add_argument("--backtest-to", default=None,
                    help="同 終了日 (既定: 当日)")
    ap.add_argument("--no-backtest", action="store_true",
                    help="バックテスト用行列を読まない (起動を最速にする)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--weights", default=None,
                    help="プリセット重みファイル (既定: out/cache/preset_weights.json。"
                         "同時学習と列別学習を同じUIで見比べるために指定できる)")
    args = ap.parse_args()

    # 既定の「今日」はここだけでシステム時刻を使う。PIT には影響しない
    # (過去走の絞り込みは対象レースの開催日を基準に model._past_runs が行う)。
    date = args.date or time.strftime("%Y%m%d")
    _STATE["date"] = date
    _STATE["preview"] = args.preview
    _STATE["specs"] = sp.maib_all_specs()
    _STATE["preset"] = ps.load_presets(args.weights)
    logger.info("プリセット重み: %s (%s)", args.weights or cfgmod.PRESET_WEIGHTS_PATH,
                _STATE["preset"].get("method", "joint"))
    # 旧モデル (別の列構成で学習した重み) を掴む事故を起動時に検出する
    col_ids = [c["id"] for c in mxmod._columns(_STATE["specs"])]
    problem = ps.check_preset_matches_spec(_STATE["preset"], col_ids)
    _STATE["preset_problem"] = problem
    if problem:
        logger.warning("プリセット重みの問題: %s (%s)", problem["code"], problem["message"])
        logger.warning("  → %s", problem["hint"])

    _STATE["daily"] = md.load_daily(date, _STATE["specs"])
    if not _STATE["daily"] and args.build:
        logger.info("当日行列を構築します date=%s (36レースで約5分)", date)
        _STATE["daily"] = md.build_daily(date, _STATE["specs"],
                                         require_confirmed=args.require_confirmed)
    n_races = len(_STATE["daily"].get("races", []))
    logger.info("当日レース数: %d (date=%s)%s", n_races, date,
                "  ★検証モード" if args.preview else "")
    if not n_races:
        # 平日は JRA 開催が無いので「レース0件」が正常。行列未構築と区別して案内する。
        built = _built_dates()
        # いま開いている日 (レース0件) を勧めても意味がないので候補から外す
        others = [d for d in built if d != date]
        logger.warning("date=%s に対象レースがありません "
                       "(平日は JRA 開催が無いので通常です)", date)
        if others:
            logger.warning("構築済みの日付: %s", ", ".join(others))
            logger.warning("過去の開催日で画面を確認するには:")
            logger.warning("  serve.bat --date %s --preview", others[-1])
        elif built:
            logger.warning("構築済みなのは %s だけです。開催日を構築してください:",
                           ", ".join(built))
            logger.warning("  build_daily.bat --date <開催日>")
        else:
            logger.warning("先に build_daily.bat を実行してください")

    if args.backtest_from and not args.no_backtest:
        from . import matrix as mx
        _STATE["backtest_matrix"] = mx.build_matrix(
            args.backtest_from, args.backtest_to or date, _STATE["specs"])
        logger.info("バックテスト用レース数: %d (%s〜%s)",
                    len(_STATE["backtest_matrix"].get("races", [])),
                    args.backtest_from, args.backtest_to or date)
    else:
        logger.info("バックテスト用行列なし → 「これまでの成績」カードは表示されません")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    # **UI の URL を出す。** ここに /api/... を出していたため、ログの URL を開くと
    # 生の JSON が表示されて「画面が開けない」という迷い方をした。
    logger.info("=" * 58)
    logger.info("  ブラウザで開いてください →  http://%s:%d/", args.host, args.port)
    logger.info("  終了は Ctrl+C")
    logger.info("=" * 58)
    logger.info("(API を直接見る場合: http://%s:%d/api/races/today — JSON が返ります)",
                args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
