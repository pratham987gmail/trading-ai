@echo off
rem Daily Upstox login (SEBI requires a fresh 2FA login every day; tokens expire ~3:30 am).
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m research.cli.upstox_login
echo.
echo Done. The bot will use the new token at 15:10 and 15:40 today.
pause
