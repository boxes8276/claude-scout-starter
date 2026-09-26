@echo off
REM Windows Task Scheduler wrapper. Edit the path to match your install.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scout.py
) else (
    python scout.py
)
