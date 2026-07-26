"""当日の基底列 (425列) を事前構築する — 当日朝に実行する唯一の準備作業。

設計書 v0.3 §9: 「事前準備は当日朝に matrix_daily を実行しておくのみ」。
実測で **1日分 36レースの構築に約5分** かかるため、発走直前に
`python -m builder.api --build` で構築を始めると待たされる。朝に済ませておけば
API は即起動する (同じキャッシュを読む)。

**システムの python では動かない** (32bit・numpy 無し)。`build_daily.bat` が
keiba-yosou の 64bit venv を使うので、そちらから呼ぶこと。

    build_daily.bat                              今日ぶんを構築
    build_daily.bat --date 20260802              日付を指定
    build_daily.bat --date 20260802 --rebuild    作り直す
    build_daily.bat --date 20250705 --require-confirmed   確定済みの過去日で試す

長時間ジョブなので、アプリと一緒に落ちないよう独立プロセスで起動することを推奨
(docs/LONG_RUNNING_JOBS.md)。原子的書き込みなので途中で落ちてもキャッシュは壊れない。
"""

from __future__ import annotations

import argparse
import time

from builder import matrix_daily as md, memprobe
from builder.specs import maib_all_specs


def main() -> int:
    ap = argparse.ArgumentParser(description="当日の基底列を事前構築する (read-only)")
    ap.add_argument("--date", default=None,
                    help="対象日 YYYYMMDD (既定: 今日。システム時刻を使う)")
    ap.add_argument("--rebuild", action="store_true",
                    help="既存キャッシュを無視して作り直す (確定後に結果を取り込む等)")
    ap.add_argument("--require-confirmed", action="store_true",
                    help="確定済みレースのみ (過去日で試すとき)")
    args = ap.parse_args()

    # 既定の「今日」はここだけでシステム時刻を使う。PIT には影響しない
    # (過去走の絞り込みは対象レースの開催日を基準に model._past_runs が行う)。
    date = args.date or time.strftime("%Y%m%d")
    specs = maib_all_specs()

    cached = md.load_daily(date, specs)
    if cached and not args.rebuild:
        print(f"すでに構築済みです date={date} レース数={len(cached.get('races', []))}")
        print("作り直す場合は --rebuild を付けてください")
        _report(cached)
        return 0

    print(f"構築します date={date} 列数={len(specs)} "
          f"(実測: 36レースで約5分かかります)", flush=True)
    t = time.time()
    daily = md.build_daily(date, specs, rebuild=args.rebuild,
                           require_confirmed=args.require_confirmed)
    print(f"完了 レース数={len(daily['races'])} {time.time()-t:.0f}s "
          f"{memprobe.fmt()}", flush=True)
    if not daily["races"]:
        print("対象日にレースがありません (開催日か、日付の指定を確認してください)")
        return 0
    _report(daily)
    print()
    print("次にこれを起動すると、このキャッシュを読んで即座に立ち上がります:")
    print(f"  python -m builder.api --date {date} --port 8780")
    return 0


def _report(daily: dict) -> None:
    """予想できる状態か・馬体重の発表待ちが何レースかを出す。"""
    status = md.today_status(daily)
    if not status:
        return
    ready = sum(1 for s in status if s["ready"])
    weighed = sum(1 for s in status if s["weight_announced"])
    rates = sorted(s["gate_pass_rate"] for s in status
                   if s["gate_pass_rate"] is not None)
    print(f"  予想できるレース: {ready}/{len(status)}")
    print(f"  馬体重が発表済み: {weighed}/{len(status)} "
          f"(未発表は発走約50分前に発表され、UI が自動で印を更新します)")
    if rates:
        print(f"  項目の利用率: 中央値 {rates[len(rates)//2]:.1%} "
              f"最小 {rates[0]:.1%} 最大 {rates[-1]:.1%}")
    tracks = {}
    for s in status:
        tracks.setdefault(s.get("track_label") or "?", []).append(s)
    print("  開催: " + " / ".join(f"{k} {len(v)}R" for k, v in tracks.items()))


if __name__ == "__main__":
    raise SystemExit(main())
