@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\update_bot_code_on_vm.ps1"
echo.
echo Finished. Log: data\botv1_live\logs\update_code.txt
timeout /t 20
