@echo off
rem Auto-create reply drafts (for Task Scheduler)
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
python auto_draft.py >> auto_draft.log 2>&1
