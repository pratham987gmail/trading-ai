@echo off
rem bot_v1 unattended run. Called by Windows Task Scheduler (TradingBot\*) at 15:10 and 15:40 on weekdays.
setlocal
cd /d "%~dp0..\.."
set BOTV1_DATA=data/botv1_live
set PYTHONIOENCODING=utf-8
if not exist "data\botv1_live\logs" mkdir "data\botv1_live\logs"
".venv\Scripts\python.exe" -m bot_v1.run auto >> "data\botv1_live\logs\scheduler.log" 2>&1
set RC=%ERRORLEVEL%
if "%RC%"=="2" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify.ps1" -Title "Trading bot: login needed" -Text "Upstox token expired, so today's run was skipped. Double-click bot_v1\scheduler\login.bat"
if "%RC%"=="1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify.ps1" -Title "Trading bot: error" -Text "See data\botv1_live\STATUS.txt"
exit /b %RC%
