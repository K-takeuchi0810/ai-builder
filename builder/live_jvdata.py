"""JRA-VAN速報馬体重(WH)の最小パーサ。公式JVData 4.9.0.1準拠。"""

from __future__ import annotations


def parse_wh(rec: bytes) -> dict:
    """WH(847 bytes)をDB更新用の形にする。無効値はNoneにする。"""
    if not rec.startswith(b"WH") or len(rec) < 845:
        return {}
    key = rec[11:27].decode("ascii", "ignore")
    if len(key) != 16 or not key.isdigit():
        return {}
    horses = []
    for i in range(18):
        pos = 35 + i * 45
        num = rec[pos:pos + 2].decode("ascii", "ignore").strip()
        weight = rec[pos + 38:pos + 41].decode("ascii", "ignore").strip()
        sign = rec[pos + 41:pos + 42].decode("ascii", "ignore").strip()
        diff = rec[pos + 42:pos + 45].decode("ascii", "ignore").strip()
        if not num.isdigit() or int(num) <= 0:
            continue
        value = int(weight) if weight.isdigit() and 2 <= int(weight) <= 998 else None
        delta = int(diff) if diff.isdigit() and 0 <= int(diff) <= 998 else None
        horses.append({"horse_num": num, "horse_weight": value,
                       "weight_change_sign": sign if sign in ("+", "-") else "",
                       "weight_change_diff": delta})
    return {"race_key": key, "announced_time":
            rec[27:35].decode("ascii", "ignore").strip(), "horses": horses}
