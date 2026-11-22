@echo off
REM Builds a single SpaceFinder.exe you can copy to any offline Windows PC (no Python needed there).
REM Run this once on a PC that has Python + internet (or pip wheels on a USB stick).
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller send2trash
python -m PyInstaller --noconfirm --onefile --windowed --name SpaceFinder ^
  --uac-admin --hidden-import send2trash main.py
echo.
echo Done: dist\SpaceFinder.exe
pause
