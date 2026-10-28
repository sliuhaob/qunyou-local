@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run 01-install.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 app.py
if errorlevel 1 pause
