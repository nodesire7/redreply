@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" goto :MISSING_ENV
"%~dp0.venv\Scripts\python.exe" -I "%~dp0tts_cli.py" %*
exit /b %errorlevel%

:MISSING_ENV
chcp 65001 >nul
>&2 echo The virtual environment is missing. Run setup_env.cmd first.
exit /b 1
