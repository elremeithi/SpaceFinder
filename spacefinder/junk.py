"""Finds worthless, corrupt and incomplete files/folders inside a chosen folder."""
from __future__ import annotations

import os
import struct
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .utils import SKIP_DIR_NAMES, is_hidden_or_system, is_link_or_reparse, is_protected

# ---------- Finding model ----------

CAT_EMPTY_FILE = "Empty file (0 bytes)"
CAT_EMPTY_FOLDER = "Empty folder"
CAT_INCOMPLETE = "Incomplete download"
CAT_CORRUPT = "Corrupt / truncated"
CAT_ZEROFILLED = "Zero-filled (broken copy)"
CAT_LEFTOVER = "Leftover / lock / temp file"
CAT_BROKEN_LINK = "Broken shortcut"
CAT_JUNK = "System junk (Thumbs.db, .DS_Store...)"


@dataclass
class Finding:
    path: str
    size: int
    category: str
    reason: str
    is_dir: bool = False
    mtime: float = 0.0
    confidence: str = "high"           # high / medium / low
    ai_verdict: str = ""               # delete / keep / review (filled by AI)
    ai_reason: str = ""
    selected: bool = field(default=True)


# ---------- Rules ----------

INCOMPLETE_EXT = {".crdownload", ".part", ".partial", ".download", ".opdownload",
                  ".!ut", ".!qb", ".bc!", ".!bt", ".aria2", ".dlpart", ".filepart"}
LEFTOVER_EXT = {".tmp", ".temp", ".chk", ".dmp", ".~tmp", ".swp", ".swo", ".old"}
JUNK_NAMES = {"thumbs.db", ".ds_store", "ehthumbs.db", "ehthumbs_vista.db", "desktop.ini.bak"}
# 0-byte files that are legitimately empty
KEEP_EMPTY_NAMES = {"__init__.py", ".gitkeep", ".keep", ".nojekyll", "py.typed", ".placeholder",
                    "__init__.pyi", ".npmignore", ".gitignore", "lock", ".lock", "nul"}

ZIP_EXT = {".zip", ".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".odt", ".ods",
           ".odp", ".epub", ".jar", ".apk", ".xpi", ".vsix", ".nupkg", ".cbz", ".3mf", ".kmz"}

SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    ".jpg": [(0, b"\xff\xd8\xff")], ".jpeg": [(0, b"\xff\xd8\xff")],
    ".png": [(0, b"\x89PNG\r\n\x1a\n")],
    ".gif": [(0, b"GIF87a"), (0, b"GIF89a")],
    ".bmp": [(0, b"BM")],
    ".webp": [(8, b"WEBP")],
    ".tif": [(0, b"II*\x00"), (0, b"MM\x00*")], ".tiff": [(0, b"II*\x00"), (0, b"MM\x00*")],
    ".pdf": [(-1, b"%PDF")],  # -1 = anywhere in first 1 KB
    ".mp4": [(4, b"ftyp")], ".m4a": [(4, b"ftyp")], ".m4v": [(4, b"ftyp")],
    ".mov": [(4, b"ftyp"), (4, b"moov"), (4, b"wide"), (4, b"mdat"), (4, b"free")],
    ".3gp": [(4, b"ftyp")], ".heic": [(4, b"ftyp")],
    ".mkv": [(0, b"\x1a\x45\xdf\xa3")], ".webm": [(0, b"\x1a\x45\xdf\xa3")],
    ".avi": [(8, b"AVI ")], ".wav": [(8, b"WAVE")],
    ".flac": [(0, b"fLaC")], ".ogg": [(0, b"OggS")], ".opus": [(0, b"OggS")],
    ".mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2"), (0, b"\xff\xfa")],
    ".7z": [(0, b"7z\xbc\xaf\x27\x1c")],
    ".rar": [(0, b"Rar!\x1a\x07")],
    ".gz": [(0, b"\x1f\x8b")], ".tgz": [(0, b"\x1f\x8b")],
    ".exe": [(0, b"MZ")], ".dll": [(0, b"MZ")], ".msi": [(0, b"\xd0\xcf\x11\xe0")],
    ".doc": [(0, b"\xd0\xcf\x11\xe0")], ".xls": [(0, b"\xd0\xcf\x11\xe0")],
    ".ppt": [(0, b"\xd0\xcf\x11\xe0")],
    ".psd": [(0, b"8BPS")], ".ico": [(0, b"\x00\x00\x01\x00")],
}
# Files we know how to verify the *end* of (truncation check)
TAIL_MARKERS = {".png": b"IEND", ".pdf": b"%%EOF", ".jpg": b"\xff\xd9", ".jpeg": b"\xff\xd9",
                ".gif": b"\x3b"}

ProgressCb = Callable[[str, int], None]


def _read(f, offset: int, n: int) -> bytes:
    f.seek(offset)
    return f.read(n)


def check_corruption(path: str, size: int, ext: str, deep_zip: bool = False) -> tuple[str, str] | None:
    """Return (category, reason) if the file looks broken, else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            # Zero-filled: header and tail are all NULs (interrupted copy / pre-allocated download)
            if size >= 4096:
                tail = _read(f, max(0, size - 4096), 4096)
                if not head.strip(b"\x00") and not tail.strip(b"\x00"):
                    return CAT_ZEROFILLED, "File contains only zero bytes - data never written"

            sigs = SIGNATURES.get(ext)
            if sigs:
                ok = False
                for off, magic in sigs:
                    if off == -1 and magic in head[:1024]:
                        ok = True
                    elif off >= 0 and head[off:off + len(magic)] == magic:
                        ok = True
                if not ok:
                    return CAT_CORRUPT, f"Header doesn't match a real {ext} file"

            marker = TAIL_MARKERS.get(ext)
            if marker and size > 64:
                tail = _read(f, max(0, size - 2048), 2048)
                if marker not in tail:
                    return CAT_CORRUPT, f"{ext} file is truncated (end marker missing)"

        if ext in ZIP_EXT:
            if not zipfile.is_zipfile(path):
                return CAT_CORRUPT, "Archive/Office file is damaged (no valid zip directory)"
            if deep_zip and size < 200 * 1024 * 1024:
                with zipfile.ZipFile(path) as z:
                    bad = z.testzip()
                    if bad:
                        return CAT_CORRUPT, f"Damaged entry inside archive: {bad}"
    except zipfile.BadZipFile:
        return CAT_CORRUPT, "Archive is damaged"
    except (PermissionError, FileNotFoundError):
        return None
    except OSError as e:
        return CAT_CORRUPT, f"Unreadable: {e.strerror or e}"
    except Exception:
        return None
    return None


def lnk_target(path: str) -> str | None:
    """Extract the local target path from a Windows .lnk shortcut (best effort)."""
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)
        if len(data) < 0x4C or data[:4] != b"\x4c\x00\x00\x00":
            return None
        flags = struct.unpack_from("<I", data, 0x14)[0]
        pos = 0x4C
        if flags & 0x1:  # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if not flags & 0x2:  # HasLinkInfo
            return None
        li = pos
        hdr_size = struct.unpack_from("<I", data, li + 4)[0]
        li_flags = struct.unpack_from("<I", data, li + 8)[0]
        if not li_flags & 0x1:  # VolumeIDAndLocalBasePath
            return None
        if hdr_size >= 0x24:  # unicode path available
            off = struct.unpack_from("<I", data, li + 0x1C)[0]
            end = data.find(b"\x00\x00", li + off)
            while end != -1 and (end - (li + off)) % 2:
                end = data.find(b"\x00\x00", end + 1)
            return data[li + off:end].decode("utf-16-le", "ignore")
        off = struct.unpack_from("<I", data, li + 0x10)[0]
        end = data.find(b"\x00", li + off)
        return data[li + off:end].decode("mbcs" if os.name == "nt" else "latin-1", "ignore")
    except Exception:
        return None


def _broken_shortcut(path: str) -> str | None:
    target = lnk_target(path)
    if not target or len(target) < 3:
        return None
    drive = os.path.splitdrive(target)[0]
    if drive and not os.path.exists(drive + os.sep):
        return None  # removable/unplugged drive - not necessarily broken
    if not os.path.exists(target):
        return f"Points to missing target: {target}"
    return None


# ---------- Scanner ----------

@dataclass
class JunkOptions:
    empty_files: bool = True
    empty_folders: bool = True
    incomplete: bool = True
    corrupt: bool = True
    leftovers: bool = True
    broken_shortcuts: bool = True
    system_junk: bool = True
    deep_zip_check: bool = False
    include_hidden: bool = False
    min_incomplete_age_hours: float = 24.0   # ignore downloads that may still be in progress


def scan_junk(roots: Iterable[str], opts: JunkOptions | None = None,
              progress: ProgressCb | None = None,
              cancelled: Callable[[], bool] = lambda: False) -> list[Finding]:
    opts = opts or JunkOptions()
    findings: list[Finding] = []
    now = time.time()
    counter = [0]

    def walk(d: str) -> bool:
        """Scan directory d. Returns True if d is (or became) empty of real content."""
        if cancelled():
            return False
        try:
            entries = list(os.scandir(d))
        except OSError:
            return False
        has_content = False
        for e in entries:
            if cancelled():
                return False
            if is_link_or_reparse(e):
                has_content = True
                continue
            name_l = e.name.lower()
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                has_content = True
                continue
            if is_dir:
                if name_l in SKIP_DIR_NAMES or is_protected(e.path) or \
                        (not opts.include_hidden and is_hidden_or_system(e)):
                    has_content = True
                    continue
                child_empty = walk(e.path)
                if child_empty and opts.empty_folders:
                    try:
                        mt = e.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        mt = 0
                    findings.append(Finding(e.path, 0, CAT_EMPTY_FOLDER,
                                            "Folder has no files in it (or only other empty folders)",
                                            is_dir=True, mtime=mt))
                elif not child_empty:
                    has_content = True
                continue

            counter[0] += 1
            if progress and counter[0] % 200 == 0:
                progress(e.path, counter[0])
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                has_content = True
                continue
            if not opts.include_hidden and is_hidden_or_system(e) and name_l not in JUNK_NAMES:
                has_content = True
                continue
            f = _classify_file(e.path, e.name, st.st_size, st.st_mtime, now, opts)
            if f:
                findings.append(f)
                # Only treat the folder as empty if the junk inside is safe to remove
                if f.category not in (CAT_EMPTY_FILE, CAT_JUNK):
                    has_content = True
            else:
                has_content = True
        return not has_content

    for root in roots:
        if is_protected(root):
            continue
        walk(root)

    # Remove nested empty-folder findings: keep only the top-most empty folder
    empty_dirs = sorted((f.path for f in findings if f.category == CAT_EMPTY_FOLDER), key=len)
    tops: list[str] = []
    for p in empty_dirs:
        if not any(p.startswith(t + os.sep) for t in tops):
            tops.append(p)
    top_set = set(tops)
    findings = [f for f in findings if f.category != CAT_EMPTY_FOLDER or f.path in top_set]
    # Files inside a reported empty folder are covered by the folder
    findings = [f for f in findings if f.is_dir or
                not any(f.path.startswith(t + os.sep) for t in tops)]
    return findings


def _classify_file(path: str, name: str, size: int, mtime: float, now: float,
                   opts: JunkOptions) -> Finding | None:
    name_l = name.lower()
    ext = os.path.splitext(name_l)[1]
    age_h = (now - mtime) / 3600

    if opts.system_junk and name_l in JUNK_NAMES:
        return Finding(path, size, CAT_JUNK, "OS-generated cache file, regenerated automatically", mtime=mtime)
    if opts.system_junk and name.startswith("._") and size <= 4096:
        return Finding(path, size, CAT_JUNK, "macOS resource-fork leftover", mtime=mtime)

    if opts.incomplete and ext in INCOMPLETE_EXT:
        if age_h >= opts.min_incomplete_age_hours:
            return Finding(path, size, CAT_INCOMPLETE,
                           f"Unfinished download, untouched for {age_h / 24:.0f} day(s)", mtime=mtime)
        return None

    if opts.leftovers:
        if name.startswith("~$") and size < 1024 * 1024:
            return Finding(path, size, CAT_LEFTOVER, "Office owner/lock file left after a crash", mtime=mtime)
        if name_l.startswith(".~lock.") and name_l.endswith("#"):
            return Finding(path, size, CAT_LEFTOVER, "LibreOffice lock file", mtime=mtime)
        if ext in LEFTOVER_EXT and age_h > 24:
            return Finding(path, size, CAT_LEFTOVER, f"Temporary/leftover {ext} file", mtime=mtime,
                           confidence="medium")

    if opts.empty_files and size == 0:
        if name_l in KEEP_EMPTY_NAMES:
            return None
        return Finding(path, 0, CAT_EMPTY_FILE, "File has no content at all", mtime=mtime)

    if opts.broken_shortcuts and ext == ".lnk":
        why = _broken_shortcut(path)
        if why:
            return Finding(path, size, CAT_BROKEN_LINK, why, mtime=mtime)
        return None

    if opts.corrupt and size > 0:
        res = check_corruption(path, size, ext, deep_zip=opts.deep_zip_check)
        if res:
            cat, why = res
            return Finding(path, size, cat, why, mtime=mtime,
                           confidence="high" if cat == CAT_ZEROFILLED else "medium")
    return None
