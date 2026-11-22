"""Shared helpers: sizes, safety rules, deletion (Recycle Bin or permanent)."""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _env_path(name: str, default: str) -> str:
    return os.path.normcase(os.path.abspath(os.environ.get(name, default)))


def protected_roots() -> list[str]:
    """Folders the user-file scanners must never touch (system cleaner has its own allow-list)."""
    if not IS_WINDOWS:
        return [os.path.normcase(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/proc",
                                               "/sbin", "/sys", "/usr", "/var/lib")]
    sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    return [
        _env_path("SystemRoot", sysdrive + "Windows"),
        _env_path("ProgramFiles", sysdrive + "Program Files"),
        _env_path("ProgramFiles(x86)", sysdrive + "Program Files (x86)"),
        _env_path("ProgramData", sysdrive + "ProgramData"),
        os.path.normcase(sysdrive + "$Recycle.Bin"),
        os.path.normcase(sysdrive + "System Volume Information"),
        os.path.normcase(sysdrive + "Recovery"),
        os.path.normcase(sysdrive + "Boot"),
        _env_path("APPDATA", sysdrive + "Users\\Default\\AppData\\Roaming"),
        _env_path("LOCALAPPDATA", sysdrive + "Users\\Default\\AppData\\Local"),
    ]


# Folders skipped while walking user folders (their "empty"/0-byte files are normal).
SKIP_DIR_NAMES = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".idea",
    ".vscode", "$recycle.bin", "system volume information", ".tox", ".gradle",
    "site-packages", ".cache", "appdata",
}


def is_protected(path: str | os.PathLike) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    for root in protected_roots():
        if p == root or p.startswith(root.rstrip("\\/") + os.sep):
            return True
    # Never operate on a drive root itself
    return os.path.dirname(p) == p


def is_hidden_or_system(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if IS_WINDOWS:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
        except (OSError, AttributeError):
            return False
    return False


def is_link_or_reparse(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        if IS_WINDOWS:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False
    return False


def folder_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def send_to_recycle_bin(path: str) -> None:
    """Move a file/folder to the Recycle Bin (reversible)."""
    try:
        from send2trash import send2trash  # type: ignore
        send2trash(path)
        return
    except ImportError:
        pass
    if IS_WINDOWS:
        _win_recycle(path)
    else:
        raise RuntimeError("Recycle Bin unavailable: run 'pip install send2trash'")


def _win_recycle(path: str) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(f"Recycle failed (code {rc}) for {path}")


def _clear_readonly(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def delete_path(path: str, permanent: bool = False) -> None:
    """Delete a file or folder. Default is Recycle Bin; permanent=True removes it for good."""
    if is_protected(path):
        raise PermissionError(f"Refusing to delete protected location: {path}")
    if not permanent:
        send_to_recycle_bin(path)
        return
    if os.path.isdir(path) and not os.path.islink(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly)
        else:
            shutil.rmtree(path, onerror=_clear_readonly)
    else:
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)


def unique_destination(dest: Path) -> Path:
    """Return dest, or 'name (1).ext', 'name (2).ext' ... if it already exists."""
    if not dest.exists():
        return dest
    stem, suffix, i = dest.stem, dest.suffix, 1
    while True:
        cand = dest.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") if IS_WINDOWS else os.path.expanduser("~/.config")
    d = Path(base or Path.home()) / "SpaceFinder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def disk_usage(path: str) -> tuple[int, int, int]:
    u = shutil.disk_usage(path)
    return u.total, u.used, u.free
