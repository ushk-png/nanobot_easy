@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "START_PS1=%SCRIPT_DIR%start-nanobot-easy.ps1"

if not exist "%START_PS1%" (
  echo Error: start-nanobot-easy.ps1 was not found next to start-nanobot.bat.
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%START_PS1%" %*
exit /b %ERRORLEVEL%
