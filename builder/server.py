"""最小 read-only 探索サーバ (標準ライブラリのみ、外部依存なし)。

keiba-yosou の webapp/server.py と同じ stdlib http.server パターン。軸とフィルタを
選ぶと explore() の結果 (TRAIN/HOLDOUT + reproduced フラグ) を表で返す。

usage:
    # デモデータ (keiba-yosou 不要、即動作確認用)
    python -m builder.server --demo
    # 実データ (keiba-yosou を兄弟ディレクトリに置き、DB を用意した後)
    python -m builder.server --from 20240101 --to 20251231
"""

from __future__ import annotations

import argparse
import html
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import axes as ax
from . import config
from .explore import explore

logger = logging.getLogger("builder.server")

_SAMPLES: list = []          # プロセス内キャッシュ (起動時に 1 回ロード)


def _fmt_pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _cell_html(c: dict) -> str:
    if not c or c.get("status") == "empty":
        return "<td colspan='6' class='muted'>—</td>"
    sig = "SIG" if c.get("gap_significant") else ""
    ins = " insufficient" if c.get("status") == "insufficient" else ""
    return (
        f"<td class='num{ins}'>{c['n']}</td>"
        f"<td class='num'>{_fmt_pct(c.get('mean_pred'))}</td>"
        f"<td class='num'>{_fmt_pct(c.get('actual_rate'))}</td>"
        f"<td class='num gap'>{c.get('calibration_gap', 0):+.3f} <span class='sig'>{sig}</span></td>"
        f"<td class='num'>[{_fmt_pct(c.get('ci_lo'))},{_fmt_pct(c.get('ci_hi'))}]</td>"
        f"<td class='num'>{_fmt_pct(c.get('return_pct'))}</td>"
    )


def render(report: dict) -> str:
    axis = html.escape(report["axis"])
    rows = []
    for c in report["cells"]:
        val = html.escape(str(c["value"]))
        rep = "✅" if c["reproduced"] else ""
        cls = "repro" if c["reproduced"] else ""
        rows.append(
            f"<tr class='{cls}'><td>{val} {rep}</td>{_cell_html(c['train'])}"
            f"{_cell_html(c['holdout'])}</tr>"
        )
    axis_opts = "".join(
        f"<option value='{a}'{' selected' if a == report['axis'] else ''}>{a}</option>"
        for a in ax.AXES
    )
    gtr = report["global_train"]
    ghd = report["global_holdout"]
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>予想ビルダー — 探索</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:1rem;background:#fafafa;color:#222}}
 h1{{font-size:1.1rem}} .muted{{color:#999}}
 form{{margin:.5rem 0 1rem}} select,input,button{{font-size:1rem;padding:.3rem}}
 table{{border-collapse:collapse;width:100%;font-size:.85rem}}
 th,td{{border:1px solid #ddd;padding:.25rem .4rem;text-align:left}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 th.grp{{background:#eef}} tr.repro{{background:#e8f5e9}}
 td.insufficient{{color:#bbb}} .sig{{color:#c62828;font-weight:bold}}
 .note{{font-size:.8rem;color:#666;margin:.5rem 0}}
 caption{{text-align:left;font-size:.8rem;color:#555;margin-bottom:.3rem}}
</style></head><body>
<h1>予想ビルダー — セグメント探索 <span class="muted">(read-only)</span></h1>
<form method="get" action="/explore">
 軸: <select name="axis">{axis_opts}</select>
 分割日(この日以降=HOLDOUT): <input name="split" value="{html.escape(report['split_date'])}" size="10">
 min_n: <input name="min_n" value="{report['min_n']}" size="4" class="num">
 <button type="submit">探索</button>
</form>
<p class="note">✅=<b>reproduced</b> (TRAIN で有意 かつ HOLDOUT で符号再現)。これだけが本物の
バイアス候補。SIG=mean_pred が実勝率 Wilson 区間外。灰色 n=min_n 未満 (バイアスと呼ばない)。
<b>保存された戦略は観察用であり、買い目には自動適用しない</b> (過学習事故防止)。</p>
<p class="note">全体 TRAIN: pred={_fmt_pct(gtr.get('mean_pred'))} actual={_fmt_pct(gtr.get('actual_rate'))}
 gap={gtr.get('calibration_gap',0):+.3f} n={gtr.get('n',0)} ／
 HOLDOUT: pred={_fmt_pct(ghd.get('mean_pred'))} actual={_fmt_pct(ghd.get('actual_rate'))}
 gap={ghd.get('calibration_gap',0):+.3f} n={ghd.get('n',0)}</p>
<table>
 <caption>軸: {axis} ／ 左=TRAIN・右=HOLDOUT</caption>
 <tr><th rowspan="2">値</th><th class="grp" colspan="6">TRAIN (探索)</th>
     <th class="grp" colspan="6">HOLDOUT (検証)</th></tr>
 <tr><th>n</th><th>pred</th><th>actual</th><th>gap</th><th>CI</th><th>ret%</th>
     <th>n</th><th>pred</th><th>actual</th><th>gap</th><th>CI</th><th>ret%</th></tr>
 {''.join(rows)}
</table></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path not in ("/", "/explore"):
            self.send_error(404)
            return
        axis = q.get("axis", "condition")
        split = q.get("split", config.DEFAULT_SPLIT_DATE)
        try:
            min_n = int(q.get("min_n", config.DEFAULT_MIN_N))
        except ValueError:
            min_n = config.DEFAULT_MIN_N
        report = explore(_SAMPLES, axis=axis, split_date=split, min_n=min_n)
        body = render(report).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _load_samples(args) -> list:
    if args.demo:
        from .demo import make_samples
        return make_samples()
    from .keiba_bridge import build_samples
    return build_samples(args.from_date, args.to_date, subject=args.subject)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="予想ビルダー 探索サーバ (read-only)")
    ap.add_argument("--demo", action="store_true", help="合成データで起動 (keiba-yosou 不要)")
    ap.add_argument("--from", dest="from_date", default="20240101")
    ap.add_argument("--to", dest="to_date", default="20251231")
    ap.add_argument("--subject", choices=["pick", "all"], default="pick")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    global _SAMPLES
    logger.info("loading samples (demo=%s)...", args.demo)
    _SAMPLES = _load_samples(args)
    logger.info("loaded %d samples", len(_SAMPLES))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("予想ビルダー: http://%s:%d/  (Ctrl-C で停止)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
