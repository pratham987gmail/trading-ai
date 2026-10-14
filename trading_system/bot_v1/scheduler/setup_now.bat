@echo off
rem One-time setup: pull Azure recorder data now, then install the TradingBot scheduled tasks.
cd /d "%~dp0..\.."
if not exist "data\botv1_live\logs" mkdir "data\botv1_live\logs"
set LOG=data\botv1_live\logs\setup_now.log
echo === setup %date% %time% > "%LOG%"
echo [1/2] Pulling Azure recorder data...
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\pull_from_vm.ps1" -VmHost azureuser@172.198.152.213 -KeyFile "%USERPROFILE%\.ssh\azure_vm.key" >> "%LOG%" 2>&1
echo pull exit code %ERRORLEVEL% >> "%LOG%"
echo [2/2] Installing scheduled tasks...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_tasks.ps1" >> "%LOG%" 2>&1
echo install exit code %ERRORLEVEL% >> "%LOG%"
type "%LOG%"
echo.
echo Done. You can close this window.
timeout /t 60
