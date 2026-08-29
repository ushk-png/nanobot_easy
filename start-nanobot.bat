@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "CONFIG=%SCRIPT_DIR%.local\config.json"
set "WORKSPACE=%SCRIPT_DIR%.local\workspace"

if not exist "%VENV_PY%" (
  echo Local virtual environment was not found. Running installer first...
  call "%SCRIPT_DIR%install.bat"
  if errorlevel 1 exit /b %ERRORLEVEL%
)

if not exist "%CONFIG%" (
  echo Config was not found. Running first-run setup wizard...
  "%VENV_PY%" -m nanobot onboard --config "%CONFIG%" --workspace "%WORKSPACE%" --wizard
  if errorlevel 1 exit /b %ERRORLEVEL%
)

set "PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%"
"%VENV_PY%" -m nanobot webui --config "%CONFIG%" --workspace "%WORKSPACE%" --background
exit /b %ERRORLEVEL%
