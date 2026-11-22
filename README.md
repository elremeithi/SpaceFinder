# Space Finder

**Space Finder** is a Windows app that frees up disk space and tidies up clutter. It runs **100% offline**: no internet, no cloud, no accounts, and no data ever leaves the PC.

| Tab | What it does |
|---|---|
| **Dashboard** | Shows free space on each drive, with one-click Quick Clean |
| **Junk & Corrupt Finder** | Finds empty files and folders, unfinished downloads (`.crdownload`, `.part`...), corrupt or truncated files (JPG, PNG, PDF, ZIP, DOCX/XLSX/PPTX, MP4, MP3...), zero-filled broken copies, Office lock files (`~$...`), broken shortcuts and Thumbs.db |
| **File Organizer** | Sorts the loose files in a folder into category folders, with a preview first and **Undo** afterwards |
| **Duplicates** | Finds byte-identical copies and keeps the "original" automatically (one copy is always kept) |
| **System Cleaner** | Cleans user and Windows Temp, the Windows Update cache, Delivery Optimization, crash dumps and WER reports, the thumbnail cache, browser caches (Chrome, Edge, Brave, Firefox), shader caches, and optionally old logs, Prefetch and Windows.old. Also empties the **Recycle Bin** |

## The offline "AI" (Smart Engine)
There are two engines, and both run on the PC itself:

1. **Built-in Smart Engine** (the default). It needs no setup and is instant.
   * **Junk review:** gives every finding a verdict of **Delete**, **Review** or **Keep**, with a reason.
     * Files modified recently, and names that look important (`thesis`, `tax`, `passport`, `wedding`, `backup`...), are sent to Review.
     * Folders that look like app or project structure (`src`, `assets`, `saves`...) are kept.
     * Only items it is sure about are pre-selected for deletion.
   * **Smart organizing:** goes beyond file types.
     * It spots *Screenshots*, *Camera Photos*, *WhatsApp Media*, *Invoices & Receipts*, *Finance*, *School*, *Career*, *Installers* and *Wallpapers* from file-name patterns.
     * Related files are grouped into project folders. For example, `Acme_report.docx`, `Acme_budget.xlsx` and `Acme_pitch.pptx` go into **Project - Acme**.
     * It reuses folders that already exist (for example, images go into an existing `Pictures` folder).
2. **Local LLM (optional)**. If [Ollama](https://ollama.com) is installed on the PC with a model in it (for example `ollama pull llama3.2:3b`), choose it in **Settings**.
   * It talks only to `127.0.0.1` and refuses any other host. It also bypasses proxies, so nothing can leave the machine.
   * The LLM can never upgrade a built-in "Review/Keep" verdict to "Delete".

## Safety
* Your own files go to the **Recycle Bin** by default, so they can be restored. System junk (temp files and caches) is deleted permanently.
* Windows, Program Files, ProgramData, AppData and drive roots are protected in the user-file scanners.
* Files that are in use are skipped, and every delete asks for confirmation.
* Unfinished downloads less than 24 hours old are ignored, because they may still be downloading.

## Run it
**Option A (with Python 3.10+ installed):** double-click `Run_SpaceFinder.bat`, or run:
```
python main.py
```
**Option B (offline PC without Python):** on any PC with Python, run `build_exe.bat` once. Then copy `dist\SpaceFinder.exe` to the offline PC with a USB stick. It is a single file, needs no install, and asks for Administrator rights so it can clean the Windows locations.

## Tests
```
python -m unittest discover tests
```
