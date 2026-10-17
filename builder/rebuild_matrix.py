"""共有サービスの「これまでの成績」用の行列を、keiba-yosou の改訂で古くなった月を含めて作り直す (2026-10-18)。

サービスは起動時に改訂で古くなった月を作り直さない (builder.api が build_matrix(stale_data="reuse"))。改訂は
全期間に効くことが多く、起動時に作り直すと数時間サービスが止まるため。このコマンドをサービスとは別のプロセスで
流して月のキャッシュを作り直し、終わったらサービスを再起動する。

    C:\\Users\\kizun\\dev\\keiba-yosou\\.venv64\\Scripts\\python.exe -m builder.rebuild_matrix            # 確認だけ
    C:\\Users\\kizun\\dev\\keiba-yosou\\.venv64\\Scripts\\python.exe -m builder.rebuild_matrix --apply    # 作り直す

- 期間はサービスと同じ (config.DISPLAY_BACKTEST_FROM 〜 今日)、列も同じ (specs.maib_all_specs)
- 1 か月の作り直しに 15〜25 分。全期間が古ければ数時間かかるので、開催の無い夜に流す
- サービスが同じ月を同時に作らないよう、作り直しの間はサービスを再起動しない。作り直しの間は
  out/matrix/rebuild.lock を置き (2 つ目の作り直しは断る)、サービスは起動時にこれを見て警告する
- 本番の checkout (C:\\Users\\kizun\\dev\\ai-builder) で流す。キャッシュは checkout の out/matrix に書くので、
  worktree で流しても本番のキャッシュは古いまま
- 改訂の台帳が在るのに読めないときは終了コード 2 (古い月を見逃したまま「0 個」と言わない)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime

from . import config, matrix, specs


def stale_months(from_date: str, to_date: str, spec_list: list[dict]) -> list[str]:
    """改訂か期間の途中で古くなっている月 (作り直す対象)。キャッシュの無い月も含む。"""
    chash = matrix._col_hash(matrix._columns(spec_list))
    today = matrix._today_jst()
    revisions = matrix._load_revisions(strict=True)
    return [ym for ym in matrix._months(from_date, to_date)
            if matrix.month_cache_status(ym, chash, today, revisions)[2] != "ok"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_date", default=config.DISPLAY_BACKTEST_FROM)
    ap.add_argument("--to", dest="to_date", default=None, help="既定は今日 (JST)")
    ap.add_argument("--apply", action="store_true", help="作り直す (無ければ対象の月を表示するだけ)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    to_date = args.to_date or matrix._today_jst()
    spec_list = specs.maib_all_specs()
    try:
        months = stale_months(args.from_date, to_date, spec_list)
    except matrix.RevisionLedgerError as exc:
        print(f"中止: {exc}。keiba-yosou の data_revisions.json を直してから流す", flush=True)
        return 2
    print(f"作り直す月: {len(months)} 個 {months}  (キャッシュ: {matrix._CACHE_DIR})")
    if not args.apply or not months:
        return 0
    lock = matrix.rebuild_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"中止: 別の作り直しが動いている ({lock}: {lock.read_text(encoding='utf-8', errors='replace')})。"
              "動いていないのが確かなら、このファイルを消してから流す", flush=True)
        return 3
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now(matrix._JST).isoformat(timespec="seconds"),
                            "months": months}, ensure_ascii=False))
    try:
        t0 = time.time()
        m = matrix.build_matrix(args.from_date, to_date, spec_list, stale_data="rebuild")
    finally:
        lock.unlink(missing_ok=True)
    print(f"完了: {len(m['races'])} レース ({args.from_date}〜{to_date})、{time.time() - t0:.0f} 秒。"
          "サービスを再起動すると反映される")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
