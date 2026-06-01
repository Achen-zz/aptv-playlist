@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_github.ps1" %*
exit /b %ERRORLEVEL%
