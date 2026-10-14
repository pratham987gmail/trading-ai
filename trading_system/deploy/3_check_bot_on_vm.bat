@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vm_bot_check.ps1"
timeout /t 30
