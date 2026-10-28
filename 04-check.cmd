@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run 01-install.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 local_control.py doctor --wechat
pause
