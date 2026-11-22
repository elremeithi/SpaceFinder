"""Windows system junk: temp folders, caches, crash dumps, update leftovers, Recycle Bin."""
from __future__ import annotations

import glob
import os
import re
import stat
import time
from dataclasses import dataclass, field
from typing import Callable

from .utils import IS_WINDOWS


@dataclass
class JunkLocation:
    key: str
    name: str
    description: str
    patterns: list[str]                 # folders (contents get cleaned) - env vars & globs allowed
    file_globs: list[str] = field(default_factory=lambda: ["*"])
    admin: bool = False
    default: bool = True
    min_age_hours: float = 0
    recursive: bool = True
    # scan results
    files: list[str] = field(default_factory=list)
    size: int = 0


def default_locations() -> list[JunkLocation]:
    L, W = "%LOCALAPPDATA%", "%SystemRoot%"
    chromium = lambda base: [f"{L}\\{base}\\User Data\\*\\Cache",
                             f"{L}\\{base}\\User Data\\*\\Code Cache",
                             f"{L}\\{base}\\User Data\\*\\GPUCache",
                             f"{L}\\{base}\\User Data\\*\\Service Worker\\CacheStorage",
                             f"{L}\\{base}\\User Data\\ShaderCache"]
    return [
        JunkLocation("user_temp", "User temporary files", "Files programs left in your Temp folder",
                     ["%TEMP%"], min_age_hours=24),
        JunkLocation("win_temp", "Windows temporary files", "System-wide Temp folder",
                     [f"{W}\\Temp"], admin=True, min_age_hours=24),
        JunkLocation("update_cache", "Windows Update cache", "Already-installed update downloads",
                     [f"{W}\\SoftwareDistribution\\Download"], admin=True, min_age_hours=72),
        JunkLocation("delivery_opt", "Delivery Optimization files", "Update sharing cache",
                     [f"{W}\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft\\Windows\\DeliveryOptimization\\Cache"],
                     admin=True),
        JunkLocation("crash_dumps", "Crash dumps & error reports", "Memory dumps and Windows Error Reporting files",
                     [f"{L}\\CrashDumps", f"{W}\\Minidump", f"{L}\\Microsoft\\Windows\\WER\\ReportArchive",
                      f"{L}\\Microsoft\\Windows\\WER\\ReportQueue", "%ProgramData%\\Microsoft\\Windows\\WER\\ReportArchive",
                      "%ProgramData%\\Microsoft\\Windows\\WER\\ReportQueue"]),
        JunkLocation("memory_dump", "Full memory dump", "MEMORY.DMP left after a blue screen",
                     [W], file_globs=["MEMORY.DMP"], admin=True, recursive=False),
        JunkLocation("thumbs", "Thumbnail cache", "Explorer rebuilds thumbnails automatically",
                     [f"{L}\\Microsoft\\Windows\\Explorer"], file_globs=["thumbcache_*.db", "iconcache_*.db"],
                     recursive=False),
        JunkLocation("inet_cache", "Internet cache (Edge/IE/Windows)", "Temporary internet files",
                     [f"{L}\\Microsoft\\Windows\\INetCache"]),
        JunkLocation("chrome", "Google Chrome cache", "Close Chrome first for best results",
                     chromium("Google\\Chrome")),
        JunkLocation("edge", "Microsoft Edge cache", "Close Edge first for best results",
                     chromium("Microsoft\\Edge")),
        JunkLocation("brave", "Brave cache", "", chromium("BraveSoftware\\Brave-Browser")),
        JunkLocation("firefox", "Firefox cache", "Close Firefox first",
                     [f"{L}\\Mozilla\\Firefox\\Profiles\\*\\cache2"]),
        JunkLocation("d3d", "DirectX shader cache", "Games/apps rebuild it as needed",
                     [f"{L}\\D3DSCache", f"{L}\\NVIDIA\\DXCache", f"{L}\\NVIDIA\\GLCache", f"{L}\\AMD\\DxCache"]),
        JunkLocation("win_logs", "Old Windows setup/log files", "CBS and DISM logs, setup leftovers",
                     [f"{W}\\Logs\\CBS", f"{W}\\Logs\\DISM", f"{W}\\Panther"], file_globs=["*.log", "*.cab", "*.etl"],
                     admin=True, min_age_hours=24 * 7, default=False),
        JunkLocation("prefetch", "Prefetch data", "Rebuilt by Windows; cleaning may slow next app launch",
                     [f"{W}\\Prefetch"], file_globs=["*.pf"], admin=True, default=False),
        JunkLocation("old_windows", "Previous Windows installation", "Windows.old (can't roll back after deleting)",
                     ["%SystemDrive%\\Windows.old"], admin=True, default=False),
    ]


_VAR = re.compile(r"%([^%]+)%")


def _expand(pattern: str) -> list[str]:
    missing = False

    def sub(m):
        nonlocal missing
        v = os.environ.get(m.group(1))
        if v is None:
            missing = True
            return ""
        return v
    path = _VAR.sub(sub, pattern)
    if missing:
        return []
    if not IS_WINDOWS:
        path = path.replace("\\", "/")
    return [p for p in glob.glob(path) if os.path.isdir(p)]


def _walk_files(folder: str, globs: list[str], recursive: bool, min_age_h: float, now: float):
    if recursive and globs == ["*"]:
        for root, dirs, files in os.walk(folder, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for f in files:
                p = os.path.join(root, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if min_age_h and (now - st.st_mtime) / 3600 < min_age_h:
                    continue
                yield p, st.st_size
    else:
        for g in globs:
            for p in glob.glob(os.path.join(folder, "**" if recursive else "", g), recursive=recursive):
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if min_age_h and (now - st.st_mtime) / 3600 < min_age_h:
                    continue
                yield p, st.st_size


def scan_locations(locs: list[JunkLocation], progress: Callable[[str, int], None] | None = None,
                   cancelled: Callable[[], bool] = lambda: False) -> None:
    now = time.time()
    for loc in locs:
        loc.files, loc.size = [], 0
        if progress:
            progress(f"Scanning {loc.name}...", 0)
        for pat in loc.patterns:
            for folder in _expand(pat):
                for p, size in _walk_files(folder, loc.file_globs, loc.recursive, loc.min_age_hours, now):
                    if cancelled():
                        return
                    loc.files.append(p)
                    loc.size += size


def clean_location(loc: JunkLocation, progress: Callable[[str, int], None] | None = None) -> tuple[int, int, int]:
    """Permanently deletes the scanned files. Files in use are skipped.
    Returns (deleted_count, freed_bytes, skipped_count)."""
    deleted = freed = skipped = 0
    for i, p in enumerate(loc.files):
        try:
            size = os.lstat(p).st_size
            try:
                os.remove(p)
            except PermissionError:
                os.chmod(p, stat.S_IWRITE)
                os.remove(p)
            deleted += 1
            freed += size
        except OSError:
            skipped += 1          # in use / access denied - normal for temp folders
        if progress and i % 200 == 0:
            progress(f"Cleaning {loc.name}: {os.path.basename(p)}", i)
    # remove now-empty sub-folders (never the root folder itself)
    for pat in loc.patterns:
        for folder in _expand(pat):
            if loc.recursive and loc.file_globs == ["*"]:
                for root, dirs, files in os.walk(folder, topdown=False, onerror=lambda e: None):
                    if os.path.normcase(root) == os.path.normcase(folder):
                        continue
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
    loc.files, loc.size = [], 0
    return deleted, freed, skipped


# ---------------- Recycle Bin (Windows shell API) ----------------

def recycle_bin_info() -> tuple[int, int]:
    """Returns (bytes, item_count) across all drives."""
    if not IS_WINDOWS:
        return 0, 0
    import ctypes

    class SHQUERYRBINFO(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            _pack_ = 1
        _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64),
                    ("i64NumItems", ctypes.c_int64)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    rc = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if rc != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin() -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes
    SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
    rc = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None,
                                                   SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    rc &= 0xFFFFFFFF
    return rc in (0, 0x8000FFFF)  # S_OK, or E_UNEXPECTED = already empty
