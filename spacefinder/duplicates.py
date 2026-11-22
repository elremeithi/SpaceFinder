"""Duplicate finder: size -> partial hash -> full hash. Byte-identical matches only."""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .utils import SKIP_DIR_NAMES, is_hidden_or_system, is_link_or_reparse, is_protected

CHUNK = 1024 * 1024
PARTIAL = 64 * 1024


@dataclass
class DupFile:
    path: str
    size: int
    mtime: float
    keep: bool = False


@dataclass
class DupGroup:
    digest: str
    size: int
    files: list[DupFile] = field(default_factory=list)

    @property
    def wasted(self) -> int:
        return self.size * (len(self.files) - 1)


def _hash(path: str, limit: int | None = None) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as f:
            if limit:
                h.update(f.read(limit))
                # also sample the tail - catches files with identical headers (e.g. same camera)
                size = os.fstat(f.fileno()).st_size
                if size > limit * 2:
                    f.seek(size - limit)
                    h.update(f.read(limit))
            else:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


_COPY_RE = re.compile(r"(\(\d+\)|\bcopy\b|- copy|_copy|\bduplicate\b|\bkopie\b)", re.I)
_TEMPISH = ("\\downloads\\", "/downloads/", "\\temp\\", "/tmp/", "\\desktop\\", "/desktop/")


def _keep_score(f: DupFile) -> tuple:
    """Lower is better. Prefer originals in organized locations, older, shorter paths."""
    name = os.path.basename(f.path)
    lower = f.path.lower()
    return (
        1 if _COPY_RE.search(name) else 0,
        1 if any(t in lower for t in _TEMPISH) else 0,
        f.mtime,
        len(f.path),
    )


def choose_keepers(groups: list[DupGroup], rule: str = "smart") -> None:
    for g in groups:
        for f in g.files:
            f.keep = False
        if rule == "newest":
            best = max(g.files, key=lambda f: f.mtime)
        elif rule == "oldest":
            best = min(g.files, key=lambda f: f.mtime)
        elif rule == "shortest":
            best = min(g.files, key=lambda f: (len(f.path), f.path))
        else:
            best = min(g.files, key=_keep_score)
        best.keep = True


def find_duplicates(roots: Iterable[str], min_size: int = 1024,
                    extensions: set[str] | None = None,
                    include_hidden: bool = False,
                    progress: Callable[[str, int], None] | None = None,
                    cancelled: Callable[[], bool] = lambda: False) -> list[DupGroup]:
    by_size: dict[int, list[tuple[str, float]]] = defaultdict(list)
    seen: set[tuple[int, int]] = set()   # (dev, inode) -> skip hard links / overlapping roots
    count = 0
    stack = [r for r in roots if not is_protected(r)]
    while stack:
        if cancelled():
            return []
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            if is_link_or_reparse(e):
                continue
            if not include_hidden and is_hidden_or_system(e):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in SKIP_DIR_NAMES and not is_protected(e.path):
                        stack.append(e.path)
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size < min_size:
                continue
            if extensions and os.path.splitext(e.name)[1].lower() not in extensions:
                continue
            key = (st.st_dev, st.st_ino)
            if st.st_ino and key in seen:
                continue
            seen.add(key)
            by_size[st.st_size].append((e.path, st.st_mtime))
            count += 1
            if progress and count % 500 == 0:
                progress(f"Indexed {count:,} files...", count)

    candidates = {s: fs for s, fs in by_size.items() if len(fs) > 1}
    total = sum(len(v) for v in candidates.values())
    done = 0
    groups: list[DupGroup] = []
    for size, files in candidates.items():
        partial: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for p, mt in files:
            if cancelled():
                return []
            done += 1
            if progress and done % 50 == 0:
                progress(f"Comparing {done:,}/{total:,}: {p}", done)
            h = _hash(p, PARTIAL)
            if h:
                partial[h].append((p, mt))
        for plist in partial.values():
            if len(plist) < 2:
                continue
            if size <= PARTIAL * 2:   # partial hash already covered the whole file
                full = {"whole": plist}
            else:
                full = defaultdict(list)
                for p, mt in plist:
                    if cancelled():
                        return []
                    h = _hash(p)
                    if h:
                        full[h].append((p, mt))
            for digest, flist in full.items():
                if len(flist) > 1:
                    groups.append(DupGroup(digest, size, [DupFile(p, size, mt) for p, mt in flist]))
    groups.sort(key=lambda g: g.wasted, reverse=True)
    choose_keepers(groups)
    return groups
