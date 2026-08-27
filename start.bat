@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM doc-translator one-click launcher: on first run it creates the virtual environment
REM and installs dependencies automatically.

where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python not found. Install Python 3.10+ and check "Add to PATH".
    echo        Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo [error] Failed to create virtual environment & pause & exit /b 1 )
    echo [setup] Installing dependencies (first run takes about 1-2 minutes)...
    ".venv\Scripts\pip.exe" install -r requirements.txt -q
    if errorlevel 1 ( echo [error] Dependency install failed; check your network and retry & pause & exit /b 1 )
)

echo [start] doc-translator at http://127.0.0.1:8765
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765

endlocal
