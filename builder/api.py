"""設計書 §8 の API (標準ライブラリのみ、外部依存なし)。

    GET  /api/races/today[?date=YYYYMMDD]   当日レース一覧 + 分析可否 (馬体重待ち/ゲート未通過)
    GET  /api/result-item-review?date=YYYYMMDD 開催日の好走馬を拾えた項目一覧
    GET  /api/features                      選べる項目 (STEP1 / STEP2 の選択肢)
    POST /api/predict                       {race_id, config} → 印 + 寄与分解 + カバレッジ
    POST /api/betslip                       {race_id, selection} → 買い目 (参加者が組む)
    POST /api/smappy/qr                     {race_id, selection} → JRA公式の投票用QR
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
import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo

import qrcode

from . import auth as access
from . import betslip as bs
from . import config as cfgmod
from . import configs as cf
from . import context_trends
from . import matrix as mxmod
from . import matrix_daily as md
from . import model
from . import odds as live_odds
from . import live_status
from . import purchases
from . import predict_service as svc
from . import presets as ps
from . import specs as sp
from . import smappy

logger = logging.getLogger("builder.api")

_STATE: dict = {"date": None, "daily": {}, "preset": {}, "specs": [], "preview": False,
                "live_refresh_enabled": False}
_AUTH: access.AuthStore | None = None
_AUTH_ENABLED = False
_ADMIN_PASSWORD = ""
_PUBLIC_URL = ""
ADMIN_OWNER_ID = "role:admin"
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN_WINDOW_SECONDS = 600
_LOGIN_MAX_FAILURES = 10
_DAILY_REFRESH_LOCK = threading.Lock()
_TREND_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_RESULT_REVIEW_LOCK = threading.Lock()
_LIVE_REFRESH_INTERVAL_SECONDS = 25.0
# 開催が終わっていて、これ以上変わるものが無いときの間隔。
# 固定 25 秒のまま回し続けたため、**9日前の開催日を指定した常駐プロセスが4日間で
# 累計 2.1 TB を読む** 事態になった (payouts.py の索引化で読み取り自体は消したが、
# 変わらないものを確かめ続ける必要もない)。過去日でも 0B12 の速報や払戻訂正が
# 後から届くことはあるので、止めるのではなく間隔を伸ばす。
_IDLE_REFRESH_INTERVAL_SECONDS = 10 * 60.0
_EMPTY_REBUILD_INTERVAL_SECONDS = 15 * 60.0
_LAST_LIVE_REFRESH = 0.0
_LAST_EMPTY_REBUILD = 0.0


def _all_settled(daily: dict) -> bool:
    """当日の全レースが終了し、払戻まで取れている状態か。

    ここが真なら、次に変わりうるのは払戻の訂正だけ。
    """
    races = daily.get("races") or []
    if not races:
        return False
    for race in races:
        if not race.get("finished"):
            return False
        if not race.get("payouts"):
            return False
    return True


def _refresh_interval(daily: dict) -> float:
    """いまの状況に応じた更新間隔。

    当日を追っている場合は日付が変わる可能性があるので短いままにする。
    日付を固定して起動した過去日は、全レース確定後は伸ばす。
    """
    if _STATE.get("follow_today", True):
        return _LIVE_REFRESH_INTERVAL_SECONDS
    if _all_settled(daily):
        return _IDLE_REFRESH_INTERVAL_SECONDS
    return _LIVE_REFRESH_INTERVAL_SECONDS


def _live_feed_status(date: str | None = None) -> dict:
    """開催変更(0B14)が最後に正常確認できた時刻と鮮度を返す。"""
    return live_status.source("0B14", target=str(date or _STATE.get("date") or ""))


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
    out["started"] = False
    out["result"] = []
    return out


def _refresh_current_daily(*, force: bool = False, today: str | None = None) -> None:
    """Refresh current race-day values and roll an always-on server to today.

    The expensive historical feature matrix remains cached. ``matrix_daily.refresh_live``
    only overlays mutable race-day fields, so this can run throughout the day.
    """
    global _LAST_LIVE_REFRESH, _LAST_EMPTY_REBUILD
    if (not force and not _STATE.get("live_refresh_enabled")) or not _STATE.get("specs"):
        return
    interval = _refresh_interval(_STATE.get("daily") or {})
    now_mono = time.monotonic()
    if not force and now_mono - _LAST_LIVE_REFRESH < interval:
        return
    if not _DAILY_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        now_mono = time.monotonic()
        if not force and now_mono - _LAST_LIVE_REFRESH < interval:
            return
        target_date = (
            (today or time.strftime("%Y%m%d"))
            if _STATE.get("follow_today", True) else str(_STATE.get("date") or "")
        )
        daily = _STATE.get("daily") or {}
        if target_date and target_date != _STATE.get("date"):
            daily = md.load_daily(target_date, _STATE["specs"])
            _STATE["date"] = target_date
            _STATE["daily"] = daily
            _LAST_EMPTY_REBUILD = 0.0
            logger.info("race date rolled over automatically: %s", target_date)

        should_rebuild_empty = (
            _STATE.get("auto_build", True)
            and target_date
            and not daily.get("races")
            and (force or now_mono - _LAST_EMPTY_REBUILD >= _EMPTY_REBUILD_INTERVAL_SECONDS)
        )
        if should_rebuild_empty:
            _LAST_EMPTY_REBUILD = now_mono
            logger.info("building missing daily matrix automatically: %s", target_date)
            daily = md.build_daily(
                target_date, _STATE["specs"], rebuild=True,
                require_confirmed=bool(_STATE.get("require_confirmed")),
            )
            _STATE["daily"] = daily

        if daily.get("races"):
            _STATE["daily"] = md.refresh_live(daily)
        _LAST_LIVE_REFRESH = time.monotonic()
    except Exception:
        logger.exception("live daily refresh failed")
    finally:
        _DAILY_REFRESH_LOCK.release()


def _daily_for_date(date: str) -> dict:
    """当日は常駐データ、過去日は結果をDBから再取得したキャッシュを返す。"""
    if str(date or "") == str(_STATE.get("date") or ""):
        return _STATE.get("daily") or {}
    cache = _STATE.setdefault("_historical_daily_cache", {})
    with _HISTORY_LOCK:
        if date not in cache:
            # 予想用キャッシュは現行版だけを使う一方、過去の成績・購入履歴は
            # 版更新前の開催日も失わない。旧版を読んだ後は必ずDBの確定値で
            # horses/order/payouts を更新するため、古い結果を表示し続けない。
            loaded = md.load_historical_daily(date, _STATE["specs"])
            cache[date] = md.refresh_live(loaded, recompute_features=False) if loaded else {}
    return cache.get(date) or {}


def _live_refresh_loop() -> None:
    while True:
        _refresh_current_daily()
        time.sleep(5)


def _start_live_refresh_thread() -> threading.Thread:
    thread = threading.Thread(target=_live_refresh_loop, name="maib-live-refresh", daemon=True)
    thread.start()
    return thread

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
    # 開催履歴はキャッシュ形式の更新をまたいで残す。実際の読み込みでは
    # load_historical_daily が列ハッシュと最低限の形を検証する。
    return sorted({m.group(1) for p in d.glob("daily_v*_*.json")
                   if (m := re.match(r"daily_v\d+_(\d{8})_", p.name))})


def _race_date_catalog(today: str | None = None, *, limit: int = 12) -> dict:
    """構築済み日付のうち、実際にレースがある開催日だけを返す。"""
    current = str(today or _STATE.get("date") or "")
    items = []
    for date in _built_dates():
        # 日付ナビの生成では、レースの有無だけ分かればよい。全開催日を
        # DB再照合すると初回表示が重くなるため、過去日はキャッシュだけを読む。
        # 選択された日の画面・成績集計では _daily_for_date が確定値を再取得する。
        daily = (_daily_for_date(date) if date == current else
                 md.load_historical_daily(date, _STATE.get("specs") or []))
        races = daily.get("races") or []
        if not races:
            continue
        statuses = md.today_status(daily)
        items.append({
            "date": date,
            "race_count": len(statuses),
            "finished_count": sum(1 for race in statuses if race.get("finished")),
        })
    previous = next((item for item in reversed(items) if item["date"] < current), None)
    next_item = next((item for item in items if item["date"] > current), None)
    return {
        "today": current,
        "previous": previous,
        "next": next_item,
        "dates": items[-max(1, int(limit)):],
    }


_PERFORMANCE_RANGE_LABELS = {
    "today": "今日",
    "previous": "前回開催",
    "7d": "直近7日",
    "all": "累計",
}


def _performance_dates(range_key: str, *, owner_id: str | None = None,
                       all_users: bool = False) -> list[str]:
    """成績画面の期間指定を、集計対象の日付へ変換する。"""
    today = str(_STATE.get("date") or (_STATE.get("daily") or {}).get("date") or "")
    key = range_key if range_key in _PERFORMANCE_RANGE_LABELS else "today"
    if key == "today":
        return [today] if today else []
    if key == "previous":
        catalog = _race_date_catalog(today)
        return [catalog["previous"]["date"]] if catalog.get("previous") else []
    recorded = purchases.recorded_dates(owner_id, all_users=all_users)
    if key == "all":
        return recorded
    if not today or len(today) != 8:
        return recorded[-7:]
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=6)).strftime("%Y%m%d")
    return [date for date in recorded if start <= date <= today]


def _races_for_dates(dates: list[str]) -> list[dict]:
    """複数開催日の購入記録を払戻まで集計できるようレースを結合する。"""
    races = []
    seen = set()
    for date in dates:
        daily = _daily_for_date(str(date))
        for race in daily.get("races") or []:
            race_id = str(race.get("race_id") or "")
            if race_id and race_id in seen:
                continue
            if race_id:
                seen.add(race_id)
            races.append(race)
    return races


def _range_meta(range_key: str, dates: list[str]) -> dict:
    key = range_key if range_key in _PERFORMANCE_RANGE_LABELS else "today"
    return {
        "range": key,
        "range_label": _PERFORMANCE_RANGE_LABELS[key],
        "from_date": min(dates) if dates else None,
        "to_date": max(dates) if dates else None,
    }


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
    handler.send_header("Content-Security-Policy",
                        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'self'")
    handler.send_header("X-Content-Type-Options", "nosniff")
    # 招待トークンはURLに含まれるため、同一originのAPIにもRefererとして送らない。
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler: BaseHTTPRequestHandler, obj, status: int = 200,
          headers: dict[str, str] | None = None) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def configure_auth(*, enabled: bool, db_path: Path | None = None,
                   admin_password: str = "", public_url: str = "") -> None:
    """共有モードを構成する。テストと main の入口を1つにする。"""
    global _AUTH, _AUTH_ENABLED, _ADMIN_PASSWORD, _PUBLIC_URL
    _AUTH_ENABLED = bool(enabled)
    _ADMIN_PASSWORD = str(admin_password or "")
    _PUBLIC_URL = str(public_url or "").rstrip("/")
    _AUTH = access.AuthStore(db_path or (Path(cfgmod.PRESET_WEIGHTS_PATH).parent / "auth.db")) \
        if _AUTH_ENABLED else None


def _owner_id(session: dict | None) -> str | None:
    """共有運用では管理者を含め、設定の所有者を必ず一意にする。"""
    if not _AUTH_ENABLED:
        return None
    if not session:
        return None
    return ADMIN_OWNER_ID if session.get("role") == "admin" else session.get("user_id")


def _cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie") or "")
    except Exception:
        return None
    return cookie[name].value if name in cookie else None


def _session_cookie(token: str, *, clear: bool = False,
                    max_age: int | None = None) -> str:
    max_age = 0 if clear else (access.SESSION_DAYS * 86400
                               if max_age is None else max(0, int(max_age)))
    value = "" if clear else token
    return (f"maib_session={value}; Path=/; Max-Age={max_age}; HttpOnly; "
            "Secure; SameSite=Lax")


def _login_key(handler: BaseHTTPRequestHandler) -> str:
    """Tunnel配下の接続元を識別する。originはlocalhost限定で運用する前提。"""
    return (handler.headers.get("CF-Connecting-IP") or handler.client_address[0]).strip()


def _login_limited(key: str, *, failed: bool | None = None) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(key, [])
                  if now - t < _LOGIN_WINDOW_SECONDS]
        if failed is True:
            recent.append(now)
        elif failed is False:
            recent = []
        if recent:
            _LOGIN_FAILURES[key] = recent
        else:
            _LOGIN_FAILURES.pop(key, None)
        return len(recent) >= _LOGIN_MAX_FAILURES


def _qr_data_url(text: str) -> str:
    image = qrcode.make(text)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _bet_type_spec(t: dict) -> dict:
    """券種1つ分の仕様。買い方ごとに **入力欄の並び** まで返す。

    UI が段数や見出しを自前で決めると、フォーメーションの段数のような
    構造の変更が2箇所に散る。ここが唯一の出どころ。
    """
    from . import labels as lbl
    modes = []
    for m in lbl.bet_modes(t["modes"]):
        groups = [{"key": name,
                   "label": bs.group_label(t, m["key"], name),
                   "exact": need}
                  for name, need in bs.group_specs(t, m["key"])]
        modes.append({**m, "groups": groups})
    return {"key": t["key"], "label": t["label"], "desc": t["desc"],
            "size": t["size"], "unit": t["unit"],
            "ordered": bool(t.get("ordered")), "modes": modes}


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
             for k in sp.maib_participant_step2_metrics() if k in model.FEATURES]
    return {
        "step1": step1,
        "step1_groups": [{"key": g, "label": lab, "desc": d}
                         for g, lab, d in lbl.STEP1_GROUPS],
        # 後方互換の既定プリセット。新UIは starter_presets を使う。
        "starter_preset": {
            "key": "standard",
            "label": "通常レース向け",
            "desc": "実績・騎手・調子の3項目から始めます。",
            "step1": [k for k in ("fit_course", "jockey_recent_30d_top3_rate",
                                  "recent_trend_delta")
                      if k in model.FEATURES],
            "step2": [],
        },
        # 未学習期間3,702レースでクラス別に検証した入口。新馬では馬自身の
        # 戦歴を使えないため、騎手・調教師・血統だけで構成する。
        "starter_presets": [
            {
                "key": "standard", "label": "通常レース向け",
                "desc": "実績・騎手・調子を確認します。",
                "step1": ["fit_course", "jockey_recent_30d_top3_rate",
                          "recent_trend_delta"], "step2": [],
            },
            {
                "key": "debut", "label": "新馬・初出走向け",
                "desc": "馬自身の戦歴を使わず、騎手・調教師・血統で評価します。",
                "step1": ["jockey_win_rate", "jockey_track_top3_rate",
                          "trainer_recent_30d_top3_rate", "sire_surface_top3_rate",
                          "sire_distance_top3_rate"], "step2": [],
            },
            {
                "key": "maiden", "label": "未勝利向け",
                "desc": "少ない戦歴を近走実績中心で評価し、騎手と血統を補助にします。",
                "step1": ["recent_avg_finish", "last_finish",
                          "jockey_track_top3_rate", "sire_distance_top3_rate"],
                "step2": [],
            },
        ],
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
        # 買い目の券種・買い方・入力欄。**UI に文言も段数も持たせない**
        # (labels.py が語彙の正本、betslip.py が構造の正本)
        "bet_types": [_bet_type_spec(t) for t in bs.BET_TYPES],
        "bet_slip_note": lbl.BET_SLIP_NOTE,
        "bet_zoro_note": lbl.ZORO_NOTE,
        "n_base_columns": len(sp.maib_step2_specs()),
        "notes": ["重みは事前学習済み (参加者は項目を選ぶだけ)",
                  "回収率はメイン指標ではありません",
                  "賞金は「獲得本賞金」(付加賞・褒賞金を含まない)"],
    }


def handle_predict(payload: dict, *, owner_id: str | None = None) -> tuple[dict, int]:
    _refresh_current_daily()
    race_id = payload.get("race_id")
    requested_date = str(payload.get("date") or "")
    # config_id 指定にも対応する。**未知の id は 404**。
    # 空の config に落として続行すると、選択列0 = 全馬スコア0 のまま
    # 「◎」を返してしまう (入力順を順位として提示することになる)。
    cfg_id = payload.get("config_id")
    saved_cfg = cf.get_config(str(cfg_id), owner_id=owner_id) if cfg_id else None
    if cfg_id and not saved_cfg:
        return {"error": "config_not_found", "config_id": cfg_id}, 404
    if cfg_id and not payload.get("config"):
        if not saved_cfg:
            return {"error": "config_not_found", "config_id": cfg_id}, 404
        user_cfg = saved_cfg["config"]
    else:
        user_cfg = payload.get("config") or {}
    daily = _daily_for_date(requested_date or str(_STATE.get("date") or ""))
    if not daily:
        return {"error": "daily_matrix_not_built",
                "date": requested_date or _STATE.get("date")}, 409
    race = md.find_race(daily, race_id) if race_id else None
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    got = svc.predict_race(race, user_cfg, _STATE["preset"])
    feed = _live_feed_status(str(race.get("date") or requested_date or ""))
    got["live_source_checked_at"] = feed.get("checked_at")
    got["live_source_age_seconds"] = feed.get("age_seconds")
    got["live_source_fresh"] = feed.get("fresh")
    # 終了レースの着順・払戻はマイAIを持っていない利用者にも公開する。
    # 予想設定が無いことを理由に結果画面まで作成画面へ送らない。
    if payload.get("result_only"):
        if not got.get("finished"):
            return {"error": "race_not_finished", "race_id": race_id}, 409
        got["result_only"] = True
        got["warnings"] = []
        got["confidence"] = {}
        got["condition_record"] = None
        return got, 200
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
    if (cfg_id and saved_cfg and
            cf.config_hash(user_cfg) == cf.config_hash(saved_cfg["config"])):
        cf.remember_applied(owner_id, str(race.get("date") or _STATE["date"]),
                            str(race_id), str(cfg_id))
    return got, 200


def handle_result_item_review(date: str) -> tuple[dict, int]:
    """開催日の全レースを、振り返り用マイAIの選択例つきで返す。

    36レースをスマホで1件ずつ開く必要をなくすための読み取り専用集約API。
    項目の採点は ``predict_race(..., config={})`` に委ね、結果画面と同じく発走前の
    特徴量と学習済み重みだけで行う。着順は振り返る馬を上位3頭に絞るためにだけ使う。
    """
    target_date = str(date or _STATE.get("date") or "")
    if target_date == str(_STATE.get("date") or ""):
        _refresh_current_daily(force=True)
    daily = _daily_for_date(target_date)
    if not daily:
        return {"error": "daily_matrix_not_built", "date": target_date}, 409

    statuses = {str(r.get("race_id")): r for r in md.today_status(daily)}
    races = daily.get("races") or []
    # 同じ確定結果へ複数人がアクセスしても全候補の再計算を繰り返さない。着順と
    # live_revision が変われば別キーとなるので、結果取込途中の内容は残らない。
    revision = tuple(
        (str(r.get("race_id") or ""), str(r.get("live_revision") or ""),
         tuple((str(h.get("num") or ""), h.get("order")) for h in r.get("horses", [])))
        for r in races)
    cache_key = (target_date, revision)
    cache = _STATE.setdefault("_result_item_review_cache", {})
    with _RESULT_REVIEW_LOCK:
        if cache_key in cache:
            return cache[cache_key], 200

        reviewed = []
        for race in races:
            race_id = str(race.get("race_id") or "")
            summary = statuses.get(race_id) or {}
            finished = any(h.get("order") == 1 for h in race.get("horses", []))
            item = {
                "race_id": race_id,
                "race_num": race.get("race_num"),
                "start_time": race.get("start_time"),
                "race_title": race.get("race_title") or race.get("race_class")
                              or race.get("race_name"),
                "track": summary.get("track"),
                "track_label": summary.get("track_label"),
                "surface": summary.get("surface"),
                "surface_label": summary.get("surface_label"),
                "distance": summary.get("distance"),
                "condition_label": summary.get("condition_label"),
                "finished": finished,
                "top3": [],
            }
            if finished:
                prediction = svc.predict_race(race, {}, _STATE["preset"])
                pickup_analysis = prediction.get("result_pickup_analysis") or {}
                analyses = pickup_analysis.get("horses") or {}
                for placed in prediction.get("result") or []:
                    analysis = analyses.get(str(placed.get("horse_num"))) or {}
                    candidates = analysis.get("candidates") or []
                    item["top3"].append({
                        **placed,
                        "candidate": candidates[0] if candidates else None,
                        "candidate_status": analysis.get("status") or "none",
                    })
                item["review_ai"] = prediction.get("result_review_ai") or svc.result_review_ai(
                    race, _STATE["preset"], prediction.get("result") or [],
                    pickup_analysis)
            reviewed.append(item)

        reviewed.sort(key=lambda r: (
            str(r.get("track_label") or ""), int(r.get("race_num") or 0)))
        result = {
            "date": target_date,
            "race_count": len(reviewed),
            "finished_count": sum(1 for r in reviewed if r["finished"]),
            "basis": "pre_race_features",
            "mode": "retrospective_ai",
            "races": reviewed,
        }
        # 日付ごとに最新1版だけを残し、開催中のrevision変化で無制限に増やさない。
        for old_key in [k for k in cache if k[0] == target_date and k != cache_key]:
            del cache[old_key]
        cache[cache_key] = result
        return result, 200


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


def handle_leaderboard(applied: dict | None = None, *,
                       owner_id: str | None = None) -> tuple[dict, int]:
    """本日の成績比較。当日の確定レースのみ集計。

    applied: {race_id: config_id}。レースごとに適用AIを切り替えた場合、
    その対応を渡すと「実際に使ったAI」に紐づけて集計する。
    """
    from . import leaderboard as lb
    _refresh_current_daily(force=True)
    daily = _STATE["daily"]
    if not daily.get("races"):
        return {"error": "daily_matrix_not_built", "date": _STATE["date"]}, 409
    remembered = cf.applied_configs(owner_id, str(daily.get("date") or _STATE["date"]))
    if applied is not None:
        remembered.update({str(k): str(v) for k, v in applied.items()})
    return lb.build_leaderboard(daily, _STATE["preset"], applied=remembered,
                                owner_id=owner_id), 200


def handle_context_trends(race_id: str) -> tuple[dict, int]:
    """対象レースより前の履歴だけで、条件別の有効項目候補を返す。"""
    _refresh_current_daily()
    daily = _STATE.get("daily") or {}
    race = md.find_race(daily, str(race_id or ""))
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    source = _STATE.get("backtest_matrix")
    if not source or not source.get("races"):
        return {"error": "no_backtest_data",
                "message": "条件別傾向を計算する過去データがありません"}, 409
    # 長期行列は通常「前回の開催週」までで、昨日分がまだ入っていないことがある。
    # 昨日の確定済み日次行列を合流し、直近傾向だけ0件になる不整合を防ぐ。
    try:
        previous_date = (datetime.strptime(str(race.get("date") or ""), "%Y%m%d")
                         - timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        previous_date = ""
    recent_daily = _daily_for_date(previous_date) if previous_date else {}
    if recent_daily.get("races"):
        source_races = list(source.get("races") or [])
        existing_ids = {str(item.get("race_id")) for item in source_races
                        if item.get("race_id")}
        additions = [item for item in recent_daily.get("races") or []
                     if not item.get("race_id") or str(item.get("race_id")) not in existing_ids]
        source = {**source, "races": source_races + additions}
    cache = _STATE.setdefault("_context_trend_cache", {})
    key = (str(race.get("date") or ""), str(race.get("race_id") or ""),
           len(source.get("races") or []))
    with _TREND_LOCK:
        if key not in cache:
            cache[key] = context_trends.analyze(source, race)
    return cache[key], 200


def handle_user_leaderboard(range_key: str = "today") -> tuple[dict, int]:
    """発走前に登録された買い目を利用者単位で集計する。"""
    # 常駐更新スレッドと成績画面の入口でライブ値を更新する。集計API自身は
    # 保存済みスナップショットを即時に返し、利用者カードだけを待たせない。
    names = {"local": "この端末", ADMIN_OWNER_ID: "管理者"}
    if _AUTH_ENABLED and _AUTH is not None:
        names.update({str(user["id"]): str(user.get("display_name") or "利用者")
                      for user in _AUTH.list_users()})
    dates = _performance_dates(range_key, all_users=True)
    if range_key == "today":
        daily = _STATE.get("daily") or {}
        date = dates[0] if dates else str(daily.get("date") or "")
        result = purchases.user_leaderboard(date, daily.get("races") or [], names)
    else:
        result = purchases.user_leaderboard_for_dates(
            dates, _races_for_dates(dates), names)
    result.update(_range_meta(range_key, dates))
    return result, 200


def handle_roi_ranking(*, owner_id: str | None = None) -> tuple[dict, int]:
    """保存済みマイAI全部を **表示期間の回収率** で並べる。

    当日の36レースでは回収率の最小レース数 (50) に届かないので、
    ランキングは表示期間 (数千レース) で集計する。回収率は蓄積して初めて
    意味を持つ数値なので、1日単位で競わせない。
    """
    from . import roi as roimod
    src = _STATE.get("backtest_matrix")
    if not src or not src.get("races"):
        return {"error": "no_backtest_data",
                "hint": "起動時に --backtest-from を指定してください"}, 409
    entries = []
    for c in cf.list_configs(owner_id=owner_id):
        got = cf.get_config(c["id"], owner_id=owner_id)
        if not got:
            continue
        bt = svc.backtest(src, got["config"], _STATE["preset"])
        entries.append({
            "config_id": c["id"], "name": c["name"], "n_items": c["n_items"],
            "races": bt["your_ai"]["races"],
            "hit_rate_win": bt["your_ai"]["hit_rate_win"],
            "roi_stats": bt["roi_stats"], "is_baseline": False,
        })
    if entries:
        base = svc.backtest(src, {"step1": [], "step2": []}, _STATE["preset"])
        entries.append({
            "config_id": None, "name": "1番人気AI", "n_items": None,
            "races": base["baseline_favorite"]["races"],
            "hit_rate_win": base["baseline_favorite"]["hit_rate_win"],
            "roi_stats": base["baseline_roi_stats"], "is_baseline": True,
        })
    roimod.rank(entries)
    entries.sort(key=lambda e: (e["roi_rank"] is None, e["roi_rank"] or 0,
                                not e["is_baseline"]))
    return {
        "period": svc.backtest(src, {"step1": [], "step2": []},
                               _STATE["preset"])["period"],
        "entries": entries,
        "roi_note": svc._roi_note(),
        "roi_min_races": roimod.MIN_RACES_FOR_ROI,
    }, 200


def _purchase_daily_and_race(payload: dict) -> tuple[dict, dict | None]:
    """買い目操作の対象日を解決する。過去レースの事後記録にも対応する。"""
    race_id = str(payload.get("race_id") or "")
    requested_date = str(payload.get("date") or "")
    if not requested_date and len(race_id) >= 8 and race_id[:8].isdigit():
        requested_date = race_id[:8]
    current_date = str(_STATE.get("date") or "")
    if not requested_date or requested_date == current_date:
        _refresh_current_daily()
        daily = _STATE.get("daily") or {}
    else:
        daily = _daily_for_date(requested_date)
    race = next((r for r in daily.get("races", [])
                 if str(r.get("race_id") or "") == race_id), None)
    return daily, race


def handle_betslip(payload: dict) -> tuple[dict, int]:
    """参加者が指定した買い目を組む。

    **表記と点数の正本をサーバに置く**ため、UI は組み合わせを自分で作らない。
    馬単の「11→10」を UI 側で組み立て直して方向を落とした事故があったので、
    点数の計算もここに寄せている (数え間違いも同じ経路で防ぐ)。

    出走していない馬番・存在しない枠は組まない。1件の指定違いは全体を止めず、
    `skipped` に理由と直し方を入れて他の買い目は作る
    (何が起きたのか分からないまま買い目が消えるのを避ける)。
    """
    from . import labels as lbl
    race_id = str(payload.get("race_id") or "")
    daily, race = _purchase_daily_and_race(payload)
    if not daily:
        return {"error": "daily_matrix_not_built"}, 409
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    active_horses = [h for h in race.get("horses", []) if not h.get("scratched")]
    runners = [h["num"] for h in active_horses]
    frames = bs.frames_of(active_horses)
    try:
        slip, skipped = bs.build_custom(payload.get("selection") or [],
                                        runners=runners, frames=frames)
    except bs.SelectionError as err:
        return {"error": "invalid_selection", "message": str(err)}, 400
    live_odds.attach(race, slip)
    return {"race_id": race_id, "slip": slip, "skipped": skipped,
            "total": bs.total_points(slip), "total_yen": bs.total_yen(slip),
            "text": bs.as_text(slip),
            "odds_note": "オッズは取得時点の値です。未発表の組は推測せず未発表と表示します",
            "note": lbl.BET_SLIP_NOTE}, 200


def handle_purchase_record(payload: dict, *,
                           owner_id: str | None = None) -> tuple[dict, int]:
    """実際に購入した買い目を、締切後も購入記録へ追加する。

    JRAへは送信せず、スマッピーQRも生成しない。発走後登録は個人の購入・払戻
    集計には含める一方、結果を見てからの登録が混ざり得る利用者別ランキングからは
    ``registered_before_start`` により除外する。
    """
    built, status = handle_betslip(payload)
    if status != 200:
        return built, status
    if not built.get("slip"):
        return {"error": "empty_selection", "message": "保存する買い目がありません"}, 400
    if built.get("skipped"):
        return {"error": "invalid_selection",
                "message": "組めない買い目があるため保存していません",
                "skipped": built["skipped"]}, 400

    _daily, race = _purchase_daily_and_race(payload)
    if race is None:
        return {"error": "race_not_found",
                "race_id": str(payload.get("race_id") or "")}, 404
    items = []
    for entry in built["slip"]:
        for combo, text, amount in zip(
                entry["combos"], entry["texts"], entry["amounts_yen"]):
            items.append({"type": entry["key"], "label": entry["label"],
                          "combo": [int(x) for x in combo], "text": text,
                          "amount_yen": int(amount)})

    cfg_id = str(payload.get("config_id") or "") or None
    saved = cf.get_config(cfg_id, owner_id=owner_id) if cfg_id else None
    if cfg_id and not saved:
        return {"error": "config_not_found", "config_id": cfg_id}, 404
    prediction = (svc.predict_race(race, saved["config"], _STATE["preset"])
                  .get("marks", [])) if saved else []
    purchase = purchases.record(
        owner_id, race=race, config_id=cfg_id, items=items, prediction=prediction,
        config_name=saved.get("name") if saved else None,
        config_snapshot=saved.get("config") if saved else None,
        status="purchased", source="manual_record",
    )
    return {
        "ok": True, "purchase_id": purchase["id"], "race_id": race.get("race_id"),
        "purchase_status": purchase["status"], "items": items,
        "total": built["total"], "total_yen": built["total_yen"],
        "registered_before_start": purchase.get("registered_before_start", False),
        "message": "購入済みの買い目として保存しました",
    }, 201


def _race_sales_closed(race: dict, *, now: datetime | None = None) -> bool:
    """当日レースが発走時刻に達したら、JRAへ送る前に締切を明示する。"""
    date = str(race.get("date") or "")
    start_time = str(race.get("start_time") or "")
    try:
        starts = datetime.strptime(date + start_time, "%Y%m%d%H:%M").replace(
            tzinfo=ZoneInfo("Asia/Tokyo"))
    except ValueError:
        return False
    current = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return current >= starts


def handle_smappy_qr(payload: dict, *, owner_id: str | None = None) -> tuple[dict, int]:
    """買い目と金額をJRA公式スマッピーへ送り、公式生成データのQRを返す。

    1件でも組めない指定があれば部分送信しない。実資金に使えるQRなので、
    「送れた分だけ」で続行すると画面の合計とQRの内容が静かにずれる。
    """
    _refresh_current_daily()
    race_id = str(payload.get("race_id") or "")
    daily = _STATE.get("daily")
    if not daily:
        return {"error": "daily_matrix_not_built"}, 409
    race = next((r for r in daily.get("races", []) if r["race_id"] == race_id), None)
    if race is None:
        return {"error": "race_not_found", "race_id": race_id}, 404
    if _race_sales_closed(race):
        return {
            "error": "race_closed",
            "message": f"このレースは発走時刻（{race.get('start_time')}）を過ぎたため、QRを作成できません。次のレースを選んでください",
        }, 409

    active_horses = [h for h in race.get("horses", []) if not h.get("scratched")]
    runners = [h["num"] for h in active_horses]
    frames = bs.frames_of(active_horses)
    try:
        slip, skipped = bs.build_custom(payload.get("selection") or [],
                                        runners=runners, frames=frames)
    except bs.SelectionError as err:
        return {"error": "invalid_selection", "message": str(err)}, 400
    if skipped:
        return {"error": "invalid_selection",
                "message": "組めない買い目があるためQRを作成していません",
                "skipped": skipped}, 400

    feed = _live_feed_status(str(race.get("date") or ""))
    if not feed.get("fresh"):
        checked = feed.get("checked_at") or "未確認"
        return {
            "error": "live_data_stale",
            "message": ("開催変更情報の最終確認から90秒以上経過しているため、"
                        "安全のためQRを作成しません。1分ほど待って更新してください。"),
            "checked_at": checked,
            "age_seconds": feed.get("age_seconds"),
            "max_age_seconds": feed.get("max_age_seconds"),
        }, 503

    points: list[smappy.Point] = []
    items = []
    for entry in slip:
        for combo, text, amount in zip(
                entry["combos"], entry["texts"], entry["amounts_yen"]):
            points.append(smappy.Point(entry["key"], tuple(int(x) for x in combo), amount))
            items.append({"type": entry["key"], "label": entry["label"],
                          "combo": [int(x) for x in combo],
                          "text": text, "amount_yen": amount})
    seg = race.get("seg") or {}
    try:
        result = smappy.create_qr(
            date=str(race.get("date") or ""),
            track_code=str(seg.get("track") or ""),
            race_num=int(race.get("race_num") or 0),
            points=points,
        )
    except smappy.SmappyError as err:
        return {"error": err.code, "message": err.message}, err.status
    cfg_id = str(payload.get("config_id") or "") or None
    prediction = []
    saved = None
    if cfg_id:
        saved = cf.get_config(cfg_id, owner_id=owner_id)
        if saved:
            prediction = svc.predict_race(race, saved["config"], _STATE["preset"]).get(
                "marks", [])
    purchase = purchases.record(
        owner_id, race=race, config_id=cfg_id, items=items, prediction=prediction,
        config_name=saved.get("name") if saved else None,
        config_snapshot=saved.get("config") if saved else None,
    )
    return {
        "race_id": race_id,
        **result,
        "items": items,
        "purchase_id": purchase["id"],
        "purchase_status": purchase["status"],
        "message": "JRAが受け付けた内容と送信内容の一致を確認しました",
    }, 200


def handle_backtest(payload: dict, owner_id: str | None = None) -> tuple[dict, int]:
    """バックテスト。**封印期間は明示的に開けるまで返さない。**

    参加者はこの数字を見ながら項目を選び直すので、見ている期間の成績は
    選び直した回数のぶんだけ楽観側に寄る。封印側を返さないことで、
    「うっかり見えてしまう」経路を塞ぐ (隠す責任は predict_service に置く)。

    `config_id` が付いていれば、その世代の調整側スコアを記録する。
    あとから「何世代目でどう動いたか」を画面に出すため。
    """
    user_cfg = payload.get("config") or {}
    period = payload.get("period") or {}
    src = _STATE.get("backtest_matrix") or _STATE["daily"]
    if not src.get("races"):
        return {"error": "no_backtest_data",
                "hint": "起動時に --backtest-from/--backtest-to を指定してください"}, 409

    config_id = str(payload.get("config_id") or "")
    reveal = bool(payload.get("reveal"))
    revealed = None
    if reveal:
        if not config_id:
            return {"error": "config_id_required",
                    "message": "封印期間を開けるには保存済みのマイAIが必要です"}, 400
        cur = cf.get_config(config_id, owner_id=owner_id)
        if not cur:
            return {"error": "config_not_found", "config_id": config_id}, 404
        revealed = cf.reveal_holdout(config_id, cur["version"], owner_id=owner_id)

    got = svc.backtest(src, user_cfg, _STATE["preset"],
                       date_from=period.get("from"),
                       date_to=period.get("to", "99999999"),
                       holdout_from=cfgmod.DISPLAY_HOLDOUT_FROM,
                       include_holdout=bool(revealed))

    if config_id:
        cur = cf.get_config(config_id, owner_id=owner_id)
        if cur:
            ai = got.get("your_ai") or {}
            cf.record_score(config_id, cur["version"],
                            {"races": ai.get("races"),
                             "win_rate": ai.get("hit_rate_win"),
                             "show_rate": ai.get("hit_rate_show")},
                            owner_id=owner_id)
            got["generations"] = cur["version"]
            got["score_history"] = cf.score_history(config_id, owner_id=owner_id)
        state = cf.holdout_state(config_id, owner_id=owner_id)
        if state and got.get("holdout") is not None:
            got["holdout"]["revealed_at_version"] = state.get("version")
            got["holdout"]["revealed_at"] = state.get("at")
    return got, 200


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

    def _session(self) -> dict | None:
        if not _AUTH_ENABLED:
            return {"user_id": None, "role": "local", "display_name": "ローカル利用者"}
        return _AUTH.get_session(_cookie_value(self, "maib_session")) if _AUTH else None

    def _require(self, role: str | None = None) -> dict | None:
        session = self._session()
        if session is None:
            _json(self, {"error": "authentication_required",
                         "message": "招待QRからログインしてください"}, 401)
            return None
        if role and session.get("role") != role:
            _json(self, {"error": "forbidden", "message": "管理者権限が必要です"}, 403)
            return None
        return session

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/")

        if path == "/api/health":
            feed = _live_feed_status()
            return _json(self, {"ok": True, "date": _STATE.get("date"),
                                "started_at": _BOOT_TIME,
                                "live_updated_at": (_STATE.get("daily") or {}).get(
                                    "live_updated_at"),
                                "live_source": feed})

        if path == "/api/auth/status":
            session = self._session()
            return _json(self, {"enabled": _AUTH_ENABLED,
                                "authenticated": session is not None,
                                "user": session,
                                "server_now": int(time.time())})

        if path == "/api/admin/invites":
            if not self._require("admin"):
                return
            return _json(self, {"invites": _AUTH.list_invites()})

        if path == "/api/admin/users":
            if not self._require("admin"):
                return
            return _json(self, {"users": _AUTH.list_users()})

        # 静的ファイルはログイン画面を表示するため公開する。業務APIだけを認証する。
        if path.startswith("/api/") and not self._require():
            return

        if path == "/api/races/today":
            _refresh_current_daily(force=q.get("refresh") == "1")
            date = q.get("date") or _STATE["date"]
            daily = _daily_for_date(date)
            if not daily:
                return _json(self, {"error": "daily_matrix_not_built", "date": date}, 409)
            races = md.today_status(daily)
            feed = _live_feed_status(date)
            for race in races:
                race["live_source_checked_at"] = feed.get("checked_at")
                race["live_source_age_seconds"] = feed.get("age_seconds")
                race["live_source_fresh"] = feed.get("fresh")
            if _STATE.get("preview"):
                races = [_as_upcoming(r) for r in races]
            return _json(self, {"date": date, "races": races,
                                "preview": bool(_STATE.get("preview")),
                                "server_now": int(time.time())})

        if path == "/api/race-dates":
            return _json(self, _race_date_catalog())

        if path == "/api/result-item-review":
            return _json(self, *handle_result_item_review(q.get("date") or _STATE["date"]))

        if path == "/api/features":
            return _json(self, feature_catalog())

        if path.startswith("/api/race-context/"):
            race_id = unquote(path[len("/api/race-context/"):])
            return _json(self, *handle_context_trends(race_id))

        if path == "/api/roi_ranking":
            return _json(self, *handle_roi_ranking(owner_id=_owner_id(self._session())))

        if path in ("/api/purchases", "/api/purchases/today"):
            range_key = q.get("range") or "today"
            owner = _owner_id(self._session())
            dates = _performance_dates(range_key, owner_id=owner)
            result = purchases.list_for_dates(owner, dates, _races_for_dates(dates))
            result.update(_range_meta(range_key, dates))
            return _json(self, result)

        if path in ("/api/user-leaderboard", "/api/user-leaderboard/today"):
            return _json(self, *handle_user_leaderboard(q.get("range") or "today"))

        if path == "/api/version":
            return _json(self, version_info())

        if path == "/api/leaderboard":
            return _json(self, *handle_leaderboard({}, owner_id=_owner_id(self._session())))


        if path == "/api/configs":
            session = self._session()
            owner = _owner_id(session)
            return _json(self, {"configs": cf.list_configs(
                owner_id=owner, include_archived=q.get("include_archived") == "1")})

        if path.startswith("/api/configs/"):
            rest = path[len("/api/configs/"):]
            if rest.endswith("/history"):
                got = cf.config_history(rest[: -len("/history")],
                                        owner_id=_owner_id(self._session()))
                return _json(self, got or {"error": "not_found"}, 200 if got else 404)
            got = cf.get_config(rest, owner_id=_owner_id(self._session()))
            return _json(self, got or {"error": "not_found"}, 200 if got else 404)

        # UI (素の HTML/CSS/JS)。`/` と `/web/*` を web/ から配信する。
        if path in ("", "/"):
            return _serve_static(self, "index.html")
        if path.startswith("/web/"):
            return _serve_static(self, path[len("/web/"):])

        return _json(self, {"error": "not_found", "path": path}, 404)

    def do_POST(self):  # noqa: N802
        try:
            return self._post()
        except Exception:
            # 未処理例外で接続が黙って切れると、UI からは「サーバが落ちている」と
            # 区別できない (実際に /api/betslip の NameError でそうなった)。
            logging.exception("POST %s で例外", self.path)
            return _json(self, {"error": "server_error", "path": self.path}, 500)

    def _post(self):
        path = urlparse(self.path).path.rstrip("/")
        payload = self._body()

        if path == "/api/auth/admin-login":
            if not _AUTH_ENABLED or not _AUTH or not _ADMIN_PASSWORD:
                return _json(self, {"error": "shared_mode_disabled"}, 404)
            supplied = str(payload.get("password") or "")
            login_key = _login_key(self)
            if _login_limited(login_key):
                return _json(self, {"error": "too_many_attempts",
                                    "message": "ログイン試行が多すぎます。10分後に再試行してください"}, 429)
            if not hmac.compare_digest(supplied.encode(), _ADMIN_PASSWORD.encode()):
                _login_limited(login_key, failed=True)
                return _json(self, {"error": "invalid_credentials",
                                    "message": "管理者パスワードが違います"}, 401)
            _login_limited(login_key, failed=False)
            token, user = _AUTH.create_admin_session()
            now = int(time.time())
            return _json(self, {"user": user, "server_now": now}, 200,
                         {"Set-Cookie": _session_cookie(
                             token, max_age=user["expires_at"] - now)})

        if path == "/api/auth/redeem":
            if not _AUTH_ENABLED or not _AUTH:
                return _json(self, {"error": "shared_mode_disabled"}, 404)
            try:
                token, user = _AUTH.redeem(str(payload.get("token") or ""),
                                           str(payload.get("display_name") or ""))
            except access.AuthError as err:
                return _json(self, {"error": err.code, "message": err.message}, 400)
            now = int(time.time())
            return _json(self, {"user": user, "server_now": now}, 200,
                         {"Set-Cookie": _session_cookie(
                             token, max_age=user["expires_at"] - now)})

        if path == "/api/auth/logout":
            if _AUTH:
                _AUTH.logout(_cookie_value(self, "maib_session"))
            return _json(self, {"ok": True}, 200,
                         {"Set-Cookie": _session_cookie("", clear=True)})

        if path == "/api/admin/invites":
            if not self._require("admin"):
                return
            try:
                token, invite = _AUTH.create_invite(
                    expires_hours=int(payload.get("expires_hours") or 24),
                    max_uses=int(payload.get("max_uses") or 1),
                )
            except (ValueError, access.AuthError) as err:
                return _json(self, {"error": getattr(err, "code", "invalid_request"),
                                    "message": str(err)}, 400)
            url = f"{_PUBLIC_URL}/?invite={quote(token)}"
            return _json(self, {**invite, "url": url, "qr_png": _qr_data_url(url)}, 201)

        if path == "/api/admin/invites/revoke":
            if not self._require("admin"):
                return
            ok = _AUTH.revoke_invite(str(payload.get("invite_id") or ""))
            return _json(self, {"ok": ok}, 200 if ok else 404)

        if path == "/api/admin/users/status":
            if not self._require("admin"):
                return
            try:
                ok = _AUTH.set_user_status(str(payload.get("user_id") or ""),
                                           str(payload.get("status") or ""))
            except access.AuthError as err:
                return _json(self, {"error": err.code, "message": err.message}, 400)
            return _json(self, {"ok": ok}, 200 if ok else 404)

        if path.startswith("/api/") and not self._require():
            return

        session = self._session()
        owner = _owner_id(session)
        if path == "/api/predict":
            return _json(self, *handle_predict(payload, owner_id=owner))
        if path == "/api/betslip":
            return _json(self, *handle_betslip(payload))
        if path == "/api/smappy/qr":
            return _json(self, *handle_smappy_qr(payload, owner_id=owner))
        if path == "/api/purchases/record":
            return _json(self, *handle_purchase_record(payload, owner_id=owner))
        if path == "/api/purchases/confirm":
            got = purchases.confirm(
                owner, str(payload.get("purchase_id") or ""),
                confirmed=bool(payload.get("confirmed", True)))
            return _json(self, got or {"error": "purchase_not_found"},
                         200 if got else 404)
        if path == "/api/purchases/delete":
            got = purchases.delete_record(owner, str(payload.get("purchase_id") or ""))
            return _json(self, got or {"error": "purchase_not_found"},
                         200 if got else 404)
        if path == "/api/backtest":
            return _json(self, *handle_backtest(payload, session.get("user_id")))
        if path == "/api/leaderboard":
            return _json(self, *handle_leaderboard(
                payload.get("applied") if isinstance(payload.get("applied"), dict) else {},
                owner_id=owner))
        if path == "/api/configs":
            # 既存IDを指定すればその版を繰り上げる (複数マイAIの編集用)
            try:
                saved = cf.save_config(payload.get("config") or payload,
                                       config_id=payload.get("config_id"), owner_id=owner)
            except PermissionError as err:
                return _json(self, {"error": "forbidden", "message": str(err)}, 403)
            return _json(self, saved, 201)
        if path == "/api/configs/rename":
            got = cf.rename_config(str(payload.get("config_id") or ""),
                                   payload.get("name") or "", owner_id=owner)
            return _json(self, got or {"error": "config_not_found"}, 200 if got else 404)
        if path == "/api/configs/duplicate":
            got = cf.duplicate_config(str(payload.get("config_id") or ""),
                                      payload.get("name"), owner_id=owner)
            return _json(self, got or {"error": "config_not_found"}, 201 if got else 404)
        if path == "/api/configs/archive":
            got = cf.archive_config(str(payload.get("config_id") or ""),
                                    bool(payload.get("archived", True)), owner_id=owner)
            return _json(self, got or {"error": "config_not_found"}, 200 if got else 404)
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
    ap.add_argument("--shared", action="store_true",
                    help="招待QRと利用者認証を必須にする共有運用モード")
    ap.add_argument("--public-url", default=os.environ.get("BUILDER_PUBLIC_URL", ""),
                    help="招待QRに入れる公開URL (例: https://host.tailnet.ts.net)")
    ap.add_argument("--weights", default=None,
                    help="プリセット重みファイル (既定: out/cache/preset_weights.json。"
                         "同時学習と列別学習を同じUIで見比べるために指定できる)")
    args = ap.parse_args()

    if args.shared:
        admin_password = os.environ.get("BUILDER_ADMIN_PASSWORD", "")
        if len(admin_password) < 12:
            ap.error("--shared では12文字以上の BUILDER_ADMIN_PASSWORD が必要です")
        if not args.public_url.startswith("https://"):
            ap.error("--shared では https:// から始まる --public-url が必要です")
        auth_db = Path(os.environ.get(
            "BUILDER_AUTH_DB", Path(cfgmod.PRESET_WEIGHTS_PATH).parent / "auth.db"))
        configure_auth(enabled=True, db_path=auth_db,
                       admin_password=admin_password, public_url=args.public_url)
    else:
        configure_auth(enabled=False)

    # 既定の「今日」はここだけでシステム時刻を使う。PIT には影響しない
    # (過去走の絞り込みは対象レースの開催日を基準に model._past_runs が行う)。
    date = args.date or time.strftime("%Y%m%d")
    _STATE["date"] = date
    _STATE["preview"] = args.preview
    _STATE["follow_today"] = args.date is None
    _STATE["auto_build"] = bool(args.build)
    _STATE["require_confirmed"] = bool(args.require_confirmed)
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

    _STATE["live_refresh_enabled"] = True
    _refresh_current_daily(force=True)
    _start_live_refresh_thread()
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
