@echo off
rem Moves the bot onto the Azure VM; log saved to data\botv1_live\logs\migrate.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0migrate_bot_to_vm.ps1"
timeout /t 60
