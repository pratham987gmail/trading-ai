@echo off
rem Nightly pull from the Azure VM (trading-recorder-vm): Angel One recorder data into data\cloud\,
rem and the bot's state + Upstox recorder data into data\botv1_vm\.
cd /d "%~dp0..\.."
if not exist "data\botv1_live\logs" mkdir "data\botv1_live\logs"
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\pull_from_vm.ps1" -VmHost azureuser@172.198.152.213 -KeyFile "%USERPROFILE%\.ssh\azure_vm.key" >> "data\botv1_live\logs\azure_pull.log" 2>&1
set RC1=%ERRORLEVEL%
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\pull_bot_from_vm.ps1" >> "data\botv1_live\logs\azure_pull.log" 2>&1
if not "%RC1%"=="0" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify.ps1" -Title "Trading bot: Azure pull failed" -Text "See data\botv1_live\logs\azure_pull.log"
