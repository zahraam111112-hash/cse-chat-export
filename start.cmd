@echo off
rem Double-click entry point: runs the PowerShell launcher beside this file.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\start.ps1"
if errorlevel 1 pause
endlocal
