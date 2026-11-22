"""Offline AI engine. 100% local - no internet, no cloud, no API keys.

Two engines:
  * SmartEngine  - built in, always available. Filename pattern recognition, keyword
                   scoring and name clustering. Runs instantly on any PC.
  * LocalLLM     - optional. Uses a model already installed in Ollama on THIS computer
                   (http://127.0.0.1:11434). Never contacts the internet.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Callable

from .junk import (CAT_BROKEN_LINK, CAT_CORRUPT, CAT_EMPTY_FILE, CAT_EMPTY_FOLDER,
                   CAT_INCOMPLETE, CAT_JUNK, CAT_LEFTOVER, CAT_ZEROFILLED)
from .organizer import OTHER


class AIError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Built-in offline engine
# --------------------------------------------------------------------------------------

# Words that suggest a file/folder matters to the user -> never auto-delete, ask instead.
IMPORTANT_WORDS = {
    "thesis", "dissertation", "tax", "taxes", "invoice", "contract", "passport", "visa", "id",
    "license", "licence", "certificate", "diploma", "transcript", "resume", "cv", "wedding",
    "baby", "family", "backup", "final", "signed", "bank", "statement", "medical", "insurance",
    "will", "deed", "mortgage", "salary", "payslip", "wallet", "seed", "key", "keys", "password",
    "recovery", "project", "important", "keep", "original", "masters", "archive", "photos",
}
# Folder names that are normal to be empty (app/project skeletons).
STRUCTURAL_DIRS = {"src", "bin", "obj", "build", "dist", "lib", "include", "assets", "logs",
                   "data", "config", "plugins", "mods", "saves", "save", "profiles", "cache",
                   "inbox", "outbox", "drafts", "templates", "output", "input", "res", "public"}

_TOKEN_RE = re.compile(r"[A-Za-z]+")


def _words(text: str) -> set[str]:
    # split camelCase too: "MyTaxReturn" -> my tax return
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return {w.lower() for w in _TOKEN_RE.findall(text)}


class SmartEngine:
    name = "Built-in Smart Engine (offline)"

    # ---------- Junk review ----------
    def review_junk(self, findings, progress: Callable[[int, int], None] | None = None) -> None:
        now = time.time()
        for n, f in enumerate(findings, 1):
            verdict, reason = self._judge(f, now)
            f.ai_verdict, f.ai_reason = verdict, reason
            f.selected = verdict == "delete"
            if progress and n % 500 == 0:
                progress(n, len(findings))
        if progress:
            progress(len(findings), len(findings))

    def _judge(self, f, now: float) -> tuple[str, str]:
        base = os.path.basename(f.path)
        stem = os.path.splitext(base)[0]
        words = _words(stem)
        age_days = (now - f.mtime) / 86400 if f.mtime else 999
        important = words & IMPORTANT_WORDS

        if f.category == CAT_JUNK:
            return "delete", "Regenerated automatically by the OS - always safe"
        if f.category == CAT_EMPTY_FOLDER:
            if base.lower() in STRUCTURAL_DIRS:
                return "keep", "Looks like part of an app/project structure"
            if age_days < 3:
                return "review", "Created very recently - you may be about to use it"
            if important:
                return "review", f"Name suggests it matters ('{', '.join(sorted(important))}')"
            return "delete", "Empty and unused - nothing will be lost"
        if age_days < 2 and f.category not in (CAT_ZEROFILLED,):
            return "review", "Modified in the last 2 days - may still be in use"
        if f.category == CAT_EMPTY_FILE:
            if important:
                return "review", "Empty, but the name looks important - check the real copy exists"
            return "delete", "No content at all - nothing will be lost"
        if f.category == CAT_ZEROFILLED:
            return "delete", "Only zero bytes inside - the data was never written"
        if f.category == CAT_INCOMPLETE:
            if age_days < 7:
                return "review", "Recent unfinished download - might be resumable"
            return "delete", f"Abandoned download ({age_days:.0f} days old) - can't be opened"
        if f.category == CAT_LEFTOVER:
            if base.startswith("~$") or base.lower().startswith(".~lock"):
                return "delete", "Stale lock file from a closed document"
            if f.size > 50 * 1024 * 1024 or important:
                return "review", "Large or important-looking leftover - glance before deleting"
            return "delete", "Old temporary leftover"
        if f.category == CAT_BROKEN_LINK:
            return "delete", "Shortcut target is gone - the shortcut does nothing"
        if f.category == CAT_CORRUPT:
            if important or f.size > 100 * 1024 * 1024:
                return "review", "Looks broken, but valuable - try opening it first"
            if "header" in f.reason.lower():
                return "review", "Wrong file signature - may just have the wrong extension"
            return "delete", "Truncated/damaged - won't open correctly"
        return "review", "Not sure - please check"

    # ---------- Organizer ----------
    TOPIC_RULES: list[tuple[str, re.Pattern]] = [
        ("Screenshots", re.compile(r"(screen ?shot|screen ?capture|snip|capture|^scr_|^Screenshot_)", re.I)),
        ("WhatsApp Media", re.compile(r"(-WA\d{4}|whatsapp)", re.I)),
        ("Camera Photos", re.compile(r"^(IMG|DSC|DSCN|DSCF|PXL|P|MVIMG|GOPR|DJI)[_-]?\d{3,}|^\d{8}_\d{6}", re.I)),
        ("Camera Videos", re.compile(r"^(VID|MOV|MVI|GH01|GX01)[_-]?\d{3,}", re.I)),
        ("Wallpapers", re.compile(r"(wallpaper|\b(1920x1080|2560x1440|3840x2160|4k|8k)\b)", re.I)),
        ("Invoices & Receipts", re.compile(r"(invoice|receipt|bill|order[_ -]?(no|confirmation)|payment|purchase|factura|rechnung)", re.I)),
        ("Finance", re.compile(r"(bank|statement|tax|payslip|salary|budget|loan|mortgage|w-?2|1099)", re.I)),
        ("Career", re.compile(r"(resume|\bcv\b|cover[ _-]?letter|offer[ _-]?letter|interview)", re.I)),
        ("School", re.compile(r"(assignment|homework|lecture|syllabus|exam|quiz|thesis|essay|course|semester|lab[ _-]?report)", re.I)),
        ("Personal Documents", re.compile(r"(passport|visa|license|licence|certificate|birth|marriage|\bid[ _-]?card|insurance|medical)", re.I)),
        ("Installers", re.compile(r"(setup|installer|install|_x64|_x86|win64|win32|portable)", re.I)),
        ("Memes", re.compile(r"(meme|funny|lol)", re.I)),
    ]
    # Topic folders only override these type-based categories
    TOPIC_OVERRIDABLE = {"Images", "Videos", "Documents", "PDFs", "Spreadsheets", "Presentations",
                         "Installers", "Archives", OTHER}
    TOPIC_ALLOWED_TYPES = {
        "Screenshots": {"Images"}, "Camera Photos": {"Images"}, "Wallpapers": {"Images"},
        "Memes": {"Images", "Videos"}, "Camera Videos": {"Videos"}, "WhatsApp Media": {"Images", "Videos", "Music"},
        "Installers": {"Installers", "Archives"},
    }
    STOP = {"the", "and", "for", "new", "copy", "final", "draft", "file", "document", "doc", "img",
            "image", "untitled", "version", "v", "rev", "edit", "edited", "scan", "page", "part",
            "of", "a", "to", "in", "on", "my", "pdf", "jpg", "png", "x", "min", "max"}

    def categorize(self, plan, existing_folders: list[str],
                   progress: Callable[[int, int], None] | None = None) -> None:
        existing_l = {f.lower(): f for f in existing_folders}
        # 1) cluster related documents by a shared significant word (e.g. "Acme_report", "Acme_budget")
        groups: dict[str, list] = defaultdict(list)
        doc_like = {"Documents", "PDFs", "Spreadsheets", "Presentations", OTHER}
        for item in plan:
            if item.category in doc_like:
                for w in _words(os.path.splitext(item.name)[0]):
                    if len(w) >= 3 and w not in self.STOP:
                        groups[w].append(item)
        claimed: set[int] = set()
        for word, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            items = [i for i in items if id(i) not in claimed]
            types = {i.category for i in items}
            # need 3+ files, sharing a topic across file types OR many of one type
            if len(items) >= 3 and (len(types) > 1 or len(items) >= 4):
                folder = f"Project - {word.title()}"
                for i in items:
                    i.category, i.source = folder, "ai"
                    claimed.add(id(i))
        # 2) topic patterns (files not already grouped into a project)
        topic_hits: dict[str, list] = defaultdict(list)
        for item in plan:
            if id(item) in claimed:
                continue
            stem = os.path.splitext(item.name)[0]
            for topic, rx in self.TOPIC_RULES:
                allowed = self.TOPIC_ALLOWED_TYPES.get(topic, self.TOPIC_OVERRIDABLE)
                if item.category in allowed and rx.search(stem):
                    topic_hits[topic].append(item)
                    break
        # avoid clutter: a topic folder needs 2+ files, unless that folder already exists
        for topic, items in topic_hits.items():
            if len(items) >= 2 or topic.lower() in existing_l:
                for item in items:
                    item.category, item.source = topic, "ai"
        # 3) prefer existing sub-folders with matching names (e.g. user already has "Pictures")
        synonyms = {"Images": ["pictures", "photos", "images", "pics"],
                    "Videos": ["videos", "movies", "clips"], "Music": ["music", "audio", "songs"],
                    "Documents": ["documents", "docs"], "Installers": ["installers", "setups", "programs"],
                    "Archives": ["archives", "zips", "compressed"], "PDFs": ["pdfs", "pdf"]}
        for item in plan:
            if item.category.lower() in existing_l:
                item.category = existing_l[item.category.lower()]
                continue
            for syn in synonyms.get(item.category, []):
                if syn in existing_l:
                    item.category = existing_l[syn]
                    break
        if progress:
            progress(len(plan), len(plan))


# --------------------------------------------------------------------------------------
# Optional local LLM through Ollama (localhost only)
# --------------------------------------------------------------------------------------

class LocalLLM:
    """Talks ONLY to an Ollama server on this machine. Pull a model once (e.g. while online,
    or copy the model files over USB): `ollama pull llama3.2:3b`. Afterwards it works offline."""

    BATCH = 60

    def __init__(self, model: str = "llama3.2:3b", host: str = "http://127.0.0.1:11434"):
        if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", host):
            raise AIError("Local LLM host must be localhost - Space Finder never goes online.")
        self.model, self.host = model, host
        self.name = f"Local LLM: {model} (offline)"

    def _post(self, path: str, payload: dict, timeout: float = 600) -> dict:
        req = urllib.request.Request(self.host + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        # Bypass any system proxy so nothing can leave the machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise AIError(f"Local model error {e.code}: {e.read()[:200].decode(errors='ignore')}")
        except (urllib.error.URLError, OSError):
            raise AIError("Ollama is not running on this PC. Start it, or use the built-in engine.")

    def list_models(self) -> list[str]:
        req = urllib.request.Request(self.host + "/api/tags")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=3) as r:
                return [m["name"] for m in json.loads(r.read().decode()).get("models", [])]
        except Exception:
            raise AIError("Ollama is not running on this PC.")

    def _ask(self, system: str, prompt: str, schema: dict) -> dict:
        out = self._post("/api/chat", {
            "model": self.model, "stream": False, "format": schema,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        })
        try:
            return json.loads(out["message"]["content"])
        except (KeyError, json.JSONDecodeError):
            raise AIError("Local model returned an unreadable answer - try a bigger model.")

    def review_junk(self, findings, progress=None) -> None:
        SmartEngine().review_junk(findings)  # safe baseline first
        system = ("You review files a disk cleaner flagged. For each id answer verdict "
                  "'delete' (clearly worthless), 'review' (might matter), or 'keep' (false positive), "
                  "with a reason under 15 words. Be conservative.")
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "integer"},
                                             "verdict": {"type": "string", "enum": ["delete", "review", "keep"]},
                                             "reason": {"type": "string"}},
            "required": ["id", "verdict", "reason"]}}}, "required": ["items"]}
        for s in range(0, len(findings), self.BATCH):
            chunk = findings[s:s + self.BATCH]
            rows = [{"id": i, "name": os.path.basename(f.path), "folder": f.is_dir, "bytes": f.size,
                     "flag": f.category, "detail": f.reason} for i, f in enumerate(chunk)]
            for it in self._ask(system, json.dumps(rows), schema).get("items", []):
                i = it.get("id")
                if isinstance(i, int) and 0 <= i < len(chunk) and it.get("verdict") in ("delete", "review", "keep"):
                    # never let the LLM upgrade the built-in 'review/keep' to 'delete'
                    if chunk[i].ai_verdict == "delete" or it["verdict"] != "delete":
                        chunk[i].ai_verdict, chunk[i].ai_reason = it["verdict"], it.get("reason", "")
                        chunk[i].selected = chunk[i].ai_verdict == "delete"
            if progress:
                progress(min(s + self.BATCH, len(findings)), len(findings))

    def categorize(self, plan, existing_folders, progress=None) -> None:
        system = ("Sort loose files into folders by what they are or are for (e.g. Screenshots, "
                  "Invoices & Receipts, Photos, School, Work - <project>). Reuse existing folders, "
                  "keep 4-15 folders total, Title Case, no slashes, max 40 chars.")
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "integer"}, "folder": {"type": "string"}},
            "required": ["id", "folder"]}}}, "required": ["items"]}
        chosen = set(existing_folders)
        for s in range(0, len(plan), self.BATCH):
            chunk = plan[s:s + self.BATCH]
            rows = [{"id": i, "name": p.name, "type": p.category} for i, p in enumerate(chunk)]
            out = self._ask(system, f"Folders so far: {sorted(chosen)}\nFiles: {json.dumps(rows)}", schema)
            for it in out.get("items", []):
                i, folder = it.get("id"), str(it.get("folder", "")).strip()
                if isinstance(i, int) and 0 <= i < len(chunk) and folder:
                    chunk[i].category, chunk[i].source = folder, "ai"
                    chosen.add(folder)
            if progress:
                progress(min(s + self.BATCH, len(plan)), len(plan))


def summarize_categories(plan) -> Counter:
    return Counter(p.category for p in plan if p.include)
