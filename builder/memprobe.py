"""プロセスのメモリ使用量を測る (Windows / 標準ライブラリのみ)。

長時間ジョブが OOM で落ちると数十分が無駄になる。425列 × 6年 の行列は
JSON で 4.6 GB あり、Python の dict に展開すると数倍に膨らむため、
**読み込みの途中で実測して安全弁を掛ける** ために使う。

psutil などの外部依存を足さない (このリポジトリは外部依存ゼロを維持する)。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_psapi = None
if sys.platform == "win32":
    try:
        _psapi = ctypes.WinDLL("psapi.dll")
        # argtypes/restype を明示しないと 64bit で戻り値が壊れ、常に 0 になる
        _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(_PMC), wintypes.DWORD]
        _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    except OSError:                                    # pragma: no cover
        _psapi = None


def _info() -> _PMC | None:
    if _psapi is None:
        return None
    m = _PMC()
    m.cb = ctypes.sizeof(_PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    return m if _psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb) else None


def rss_gb() -> float | None:
    """現在の working set (GB)。測れない環境では None。"""
    m = _info()
    return None if m is None else m.WorkingSetSize / 1e9


def peak_gb() -> float | None:
    """プロセス開始以降のピーク working set (GB)。"""
    m = _info()
    return None if m is None else m.PeakWorkingSetSize / 1e9


def fmt() -> str:
    r, p = rss_gb(), peak_gb()
    if r is None:
        return "mem=n/a"
    return f"mem={r:.1f}GB peak={p:.1f}GB"


def available_gb() -> float | None:
    """空き物理メモリ (GB)。安全弁の既定値を決めるのに使う。"""
    if sys.platform != "win32":
        return None

    class _MS(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    s = _MS()
    s.dwLength = ctypes.sizeof(_MS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        return None
    return s.ullAvailPhys / 1e9
