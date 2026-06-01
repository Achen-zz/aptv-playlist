@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0login_github.ps1"
exit /b %ERRORLEVEL%
