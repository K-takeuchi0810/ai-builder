"""425列行列ビルドの進捗をファイルから復元して表示する。

プロセスが死んでいても状態が分かるようにするための確認用 (docs/LONG_RUNNING_JOBS.md)。

    python scripts_build_status.py
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from builder import matrix
from builder.specs import maib_all_specs

NEED = {"2021": 12, "2022": 12, "2023": 12, "2024": 12, "2025": 12, "2026": 7}


def main() -> int:
    specs = maib_all_specs()
    cols = matrix._columns(specs)
    month_hash = matrix._col_hash(cols)

    months = sorted(Path("out/matrix/months").glob(f"m_*_{month_hash}.json"))
    per_year = Counter(p.name.split("_")[1][:4] for p in months)
    total_need = sum(NEED.values())
    size = sum(p.stat().st_size for p in months) / 1e9

    print(f"425列 ({len(cols)}列) の進捗  指紋={month_hash}")
    print(f"  月キャッシュ: {len(months)}/{total_need} ヶ月  ({size:.2f} GB)")
    for y, need in sorted(NEED.items()):
        got = per_year.get(y, 0)
        # レンジキャッシュは _cache_path (from/to を含む別ハッシュ) で名前が決まる
        rng = matrix._cache_path(f"{y}0101", f"{y}1231", cols)
        done = "✅完了" if rng.exists() else f"{got}/{need}"
        mb = f"  ({rng.stat().st_size/1e6:.0f} MB)" if rng.exists() else ""
        print(f"    {y}: {done}{mb}")

    ready = [y for y in NEED if matrix._cache_path(f"{y}0101", f"{y}1231", cols).exists()]
    print(f"  学習に使える年: {sorted(ready)}")
    tmp = list(Path("out/matrix/months").glob("*.tmp"))
    if tmp:
        print(f"  ⚠ 中間ファイル {len(tmp)} 件 (書き込み中)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
