@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "INSTALL_PS1=%SCRIPT_DIR%install-nanobot-easy.ps1"

if not exist "%INSTALL_PS1%" (
  echo Error: install-nanobot-easy.ps1 was not found next to install.bat.
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_PS1%" %*
exit /b %ERRORLEVEL%
