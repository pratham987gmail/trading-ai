@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\vm_remove_bot.ps1"
echo.
echo Finished. Log: data\vm_remove_bot.txt
pause
