@echo off
title MikroTik LAN Monitor
cd /d "%~dp0"

REM ---- find Python (py launcher first, then python) ----
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
  echo [!] Python was not found. Install it from https://www.python.org/downloads/
  echo     IMPORTANT: tick "Add python.exe to PATH" during install.
  pause
  exit /b 1
)

echo [*] Checking/installing dependencies (first run only, ~30s)...
%PYCMD% -m pip install --quiet --disable-pip-version-check librouteros fastapi uvicorn

echo [*] Starting dashboard at http://localhost:8899  (leave this window open)
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8899"
%PYCMD% server.py
pause
