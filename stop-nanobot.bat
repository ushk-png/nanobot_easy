@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "STOP_PS1=%SCRIPT_DIR%stop-nanobot-easy.ps1"

if not exist "%STOP_PS1%" (
  echo Error: stop-nanobot-easy.ps1 was not found next to stop-nanobot.bat.
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STOP_PS1%" %*
exit /b %ERRORLEVEL%
