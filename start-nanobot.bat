@echo off
setlocal

if exist "%USERPROFILE%\.nanobot\venv\Scripts\python.exe" (
  "%USERPROFILE%\.nanobot\venv\Scripts\python.exe" -m nanobot webui
  exit /b %ERRORLEVEL%
)

where uv >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  uv tool run --from nanobot-ai nanobot webui
  exit /b %ERRORLEVEL%
)

where nanobot >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  nanobot webui
  exit /b %ERRORLEVEL%
)

echo Error: nanobot was not found. Run install.bat first.
exit /b 1
