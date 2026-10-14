@echo off
rem Pulls 2 years of expired option candles (all indices) from Upstox for the condor study. Resumable.
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m bot_v1.research.pull_expired_history
echo.
pause
