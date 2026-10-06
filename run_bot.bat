@echo off
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist logs mkdir logs
.venv\Scripts\python.exe trading.py >> logs\trading.log 2>&1
