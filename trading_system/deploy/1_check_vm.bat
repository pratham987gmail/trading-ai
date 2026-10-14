@echo off
rem Read-only check of the Azure VM; report saved to data\botv1_live\logs\vm_inspect.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vm_inspect.ps1"
timeout /t 20
