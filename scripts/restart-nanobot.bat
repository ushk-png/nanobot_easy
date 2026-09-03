@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "RESTART_PS1=%SCRIPT_DIR%restart-nanobot-easy.ps1"

if not exist "%RESTART_PS1%" (
  echo Error: restart-nanobot-easy.ps1 was not found next to restart-nanobot.bat.
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RESTART_PS1%" %*
exit /b %ERRORLEVEL%
