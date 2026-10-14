@echo off
rem Nightly pull of the Azure recorder's option-chain data (trading-recorder-vm) into data\cloud\.
cd /d "%~dp0..\.."
if not exist "data\botv1_live\logs" mkdir "data\botv1_live\logs"
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\pull_from_vm.ps1" -VmHost azureuser@172.198.152.213 -KeyFile "%USERPROFILE%\.ssh\azure_vm.key" >> "data\botv1_live\logs\azure_pull.log" 2>&1
if errorlevel 1 powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify.ps1" -Title "Trading bot: Azure pull failed" -Text "See data\botv1_live\logs\azure_pull.log"
