"""File organizer: categorize loose files in a folder, preview, move, and undo."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .utils import app_data_dir, is_protected, unique_destination

CATEGORIES: dict[str, set[str]] = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
               ".heif", ".svg", ".ico", ".raw", ".cr2", ".nef", ".arw", ".dng", ".avif", ".jfif"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
               ".mpeg", ".3gp", ".ts", ".vob"},
    "Music": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".aiff", ".mid", ".midi"},
    "Documents": {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages", ".tex", ".wpd"},
    "PDFs": {".pdf"},
    "Spreadsheets": {".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv", ".numbers"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key", ".pps", ".ppsx"},
    "Ebooks": {".epub", ".mobi", ".azw", ".azw3", ".fb2", ".djvu", ".cbz", ".cbr"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".zst", ".cab"},
    "Installers": {".exe", ".msi", ".msix", ".appx", ".appxbundle", ".msixbundle", ".apk", ".dmg", ".pkg", ".deb", ".rpm"},
    "Disk Images": {".iso", ".img", ".vhd", ".vhdx", ".vmdk"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".php",
             ".rb", ".html", ".css", ".json", ".xml", ".yml", ".yaml", ".sh", ".bat", ".ps1",
             ".sql", ".ipynb", ".kt", ".swift", ".lua", ".ini", ".toml"},
    "Fonts": {".ttf", ".otf", ".woff", ".woff2", ".fon"},
    "Design & 3D": {".psd", ".ai", ".xd", ".fig", ".sketch", ".indd", ".blend", ".obj", ".fbx",
                    ".stl", ".3mf", ".dwg", ".dxf", ".skp", ".eps", ".kra", ".xcf"},
    "Torrents": {".torrent"},
}
EXT_TO_CAT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
OTHER = "Other"
SKIP_EXT_DEFAULT = {".lnk", ".url", ".ini", ".sys", ".crdownload", ".part", ".tmp"}
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


@dataclass
class PlanItem:
    src: str
    name: str
    size: int
    category: str
    source: str = "rules"     # rules / ai / user
    include: bool = True


def categorize_by_rules(name: str) -> str:
    return EXT_TO_CAT.get(os.path.splitext(name)[1].lower(), OTHER)


def build_plan(folder: str, skip_shortcuts: bool = True) -> list[PlanItem]:
    """Plan for every loose (top-level) file in folder."""
    if is_protected(folder):
        raise PermissionError("That folder is a protected system location.")
    skip_ext = set(SKIP_EXT_DEFAULT) if skip_shortcuts else SKIP_EXT_DEFAULT - {".lnk", ".url"}
    items: list[PlanItem] = []
    with os.scandir(folder) as it:
        for e in it:
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            name_l = e.name.lower()
            if name_l in SKIP_NAMES or name_l.startswith("~$") or name_l.startswith(".spacefinder"):
                continue
            if os.path.splitext(name_l)[1] in skip_ext:
                continue
            items.append(PlanItem(e.path, e.name, st.st_size, categorize_by_rules(e.name)))
    items.sort(key=lambda i: (i.category, i.name.lower()))
    return items


def existing_subfolders(folder: str) -> list[str]:
    try:
        return sorted(e.name for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)
                      and not e.name.startswith("."))
    except OSError:
        return []


def sanitize_folder_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("-" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    if cleaned.upper() in {"CON", "PRN", "AUX", "NUL"} or not cleaned:
        cleaned = OTHER
    return cleaned[:60]


def apply_plan(folder: str, plan: list[PlanItem],
               progress: Callable[[str, int], None] | None = None) -> tuple[int, list[str], Path]:
    """Move files. Returns (moved_count, errors, undo_log_path)."""
    moves: list[dict] = []
    errors: list[str] = []
    base = Path(folder)
    for i, item in enumerate(i for i in plan if i.include):
        src = Path(item.src)
        if not src.exists():
            continue
        target_dir = base / sanitize_folder_name(item.category)
        try:
            target_dir.mkdir(exist_ok=True)
            dest = unique_destination(target_dir / src.name)
            shutil.move(str(src), str(dest))
            moves.append({"from": str(src), "to": str(dest)})
        except OSError as e:
            errors.append(f"{src.name}: {e}")
        if progress:
            progress(src.name, i + 1)
    log = app_data_dir() / f"undo_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log.write_text(json.dumps({"folder": folder, "time": time.time(), "moves": moves}, indent=1),
                   encoding="utf-8")
    return len(moves), errors, log


def list_undo_logs() -> list[Path]:
    return sorted(app_data_dir().glob("undo_*.json"), reverse=True)


def undo(log_path: Path) -> tuple[int, list[str]]:
    data = json.loads(Path(log_path).read_text(encoding="utf-8"))
    restored, errors = 0, []
    created_dirs = set()
    for m in reversed(data["moves"]):
        src, dest = Path(m["to"]), Path(m["from"])
        try:
            if src.exists():
                shutil.move(str(src), str(unique_destination(dest)))
                restored += 1
                created_dirs.add(src.parent)
        except OSError as e:
            errors.append(f"{src.name}: {e}")
    for d in created_dirs:            # remove category folders we emptied
        try:
            d.rmdir()
        except OSError:
            pass
    Path(log_path).rename(Path(log_path).with_suffix(".undone"))
    return restored, errors
