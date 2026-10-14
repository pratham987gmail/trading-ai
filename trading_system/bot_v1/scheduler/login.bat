@echo off
rem Daily Upstox login (SEBI requires a fresh 2FA login every day; tokens expire ~3:30 am).
rem The new token is used here and copied to the Azure VM, where the bot and recorders run.
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m research.cli.upstox_login
echo.
echo Copying today's token to the Azure VM...
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\push_upstox_token.ps1"
echo.
echo Done. The bot runs on the Azure VM at 15:10 and 15:40 today.
pause
