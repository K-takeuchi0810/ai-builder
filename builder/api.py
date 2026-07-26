"""設計書 §8 の API (標準ライブラリのみ、外部依存なし)。

    GET  /api/races/today[?date=YYYYMMDD]   当日レース一覧 + 分析可否 (馬体重待ち/ゲート未通過)
    GET  /api/features                      選べる項目 (STEP1 / STEP2 の選択肢)
    POST /api/predict                       {race_id, config} → 印 + 寄与分解 + カバレッジ
    POST /api/backtest                      {config, period?} → 的中率系 + 人気ベースライン
    POST /api/configs                       設定の保存 (マイAI v1 → v2)
    GET  /api/configs/{id}                  設定の取得
    GET  /api/configs/{id}/history          成績推移用のバージョン履歴

起動:
    python -m builder.api --date 20260801 --port 8780

当日データは matrix_daily のキャッシュを使う (無ければ起動時に構築)。
プリセット重みは presets の保存済みファイルを読む (無ければ全重み0で動くが印は無意味)。
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
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

_STATE: dict = {"date": None, "daily": {}, "preset": {}, "specs": []}

# UI の静的ファイル (素の HTML/CSS/JS)。外部依存ゼロ・1プロセス起動のため
# フレームワークやビルドツールは使わず、ここから直接配信する (UI指示書 §0)。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
    step1 = [{"key": s["key"], "label": model.FEATURES[s["key"]].label,
              "category": model.FEATURES[s["key"]].category}
             for s in sp.maib_step1_specs()]
    step2 = [{"metric": k, "label": model.FEATURES[k].label}
             for k in sp.MAIB_STEP2_METRICS if k in model.FEATURES]
    return {
        "step1": step1,
        "step2_metrics": step2,
        "step2_matches": [{"value": m, "label": lbl} for m, lbl in
                          zip(sp.MAIB_MATCHES,
                              ["全レース", "距離が同じ", "競馬場が同じ", "芝ダートを分ける"])],
        "step2_lookbacks": [{"value": lb, "label": ("全レース" if lb is None else f"直近{lb}レース")}
                            for lb in sp.MAIB_LOOKBACKS],
        "n_base_columns": len(sp.maib_step2_specs()),
        "notes": ["重みは事前学習済み (参加者は項目を選ぶだけ)",
                  "回収率はメイン指標ではありません",
                  "賞金は「獲得本賞金」(付加賞・褒賞金を含まない)"],
    }


def handle_predict(payload: dict) -> tuple[dict, int]:
    race_id = payload.get("race_id")
    user_cfg = payload.get("config") or {}
    race = md.find_race(_STATE["daily"], race_id) if race_id else None
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    got = svc.predict_race(race, user_cfg, _STATE["preset"])
    problem = _STATE.get("preset_problem")
    if problem and problem["code"] == "preset_column_mismatch":
        got.setdefault("warnings", []).append(problem)
    return got, 200


def handle_leaderboard() -> tuple[dict, int]:
    """きょうの順位 (UI指示書 §5)。当日の確定レースのみ集計。"""
    from . import leaderboard as lb
    daily = _STATE["daily"]
    if not daily.get("races"):
        return {"error": "daily_matrix_not_built", "date": _STATE["date"]}, 409
    return lb.build_leaderboard(daily, _STATE["preset"]), 200


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
            return _json(self, {"date": date, "races": md.today_status(daily)})

        if path == "/api/features":
            return _json(self, feature_catalog())

        if path == "/api/leaderboard":
            return _json(self, *handle_leaderboard())

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
        if path == "/api/configs":
            saved = cf.save_config(payload.get("config") or payload)
            return _json(self, saved, 201)
        return _json(self, {"error": "not_found", "path": path}, 404)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="MAIBuilder API (read-only)")
    ap.add_argument("--date", required=True, help="当日日付 YYYYMMDD")
    ap.add_argument("--build", action="store_true", help="当日行列を無ければ構築する")
    ap.add_argument("--require-confirmed", action="store_true",
                    help="確定済みレースのみ (過去日で試すとき)")
    ap.add_argument("--backtest-from", default=None, help="バックテスト用行列の開始日")
    ap.add_argument("--backtest-to", default=None, help="同 終了日")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    args = ap.parse_args()

    _STATE["date"] = args.date
    _STATE["specs"] = sp.maib_all_specs()
    _STATE["preset"] = ps.load_presets()
    # 旧モデル (別の列構成で学習した重み) を掴む事故を起動時に検出する
    col_ids = [c["id"] for c in mxmod._columns(_STATE["specs"])]
    problem = ps.check_preset_matches_spec(_STATE["preset"], col_ids)
    _STATE["preset_problem"] = problem
    if problem:
        logger.warning("プリセット重みの問題: %s (%s)", problem["code"], problem["message"])
        logger.warning("  → %s", problem["hint"])

    _STATE["daily"] = md.load_daily(args.date, _STATE["specs"])
    if not _STATE["daily"] and args.build:
        logger.info("当日行列を構築します date=%s", args.date)
        _STATE["daily"] = md.build_daily(args.date, _STATE["specs"],
                                         require_confirmed=args.require_confirmed)
    logger.info("当日レース数: %d", len(_STATE["daily"].get("races", [])))

    if args.backtest_from:
        from . import matrix as mx
        _STATE["backtest_matrix"] = mx.build_matrix(
            args.backtest_from, args.backtest_to or args.backtest_from, _STATE["specs"])
        logger.info("バックテスト用レース数: %d",
                    len(_STATE["backtest_matrix"].get("races", [])))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("MAIBuilder API: http://%s:%d/api/races/today", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
