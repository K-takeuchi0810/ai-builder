"""Print today's dynamic live-task window as JSON for PowerShell."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
KEIBA = ROOT.parent / "keiba-yosou"
sys.path.insert(0, str(KEIBA))
sys.path.insert(0, str(ROOT))

from builder.live_schedule import decide  # noqa: E402
from db import open_db  # type: ignore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    args = parser.parse_args()
    with open_db() as conn:
        result = decide(conn, args.date)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
