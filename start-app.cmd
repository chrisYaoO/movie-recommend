@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start-app.ps1" %*
