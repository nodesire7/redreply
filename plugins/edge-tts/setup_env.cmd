@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "PYLAUNCHER_DRYRUN="
set "TTS_BOOT_PY="
set "TTS_CANDIDATE="

if defined TTS_PYTHON goto :CUSTOM_PYTHON

set "TTS_CANDIDATE=%~dp0.venv\Scripts\python.exe"
call :CHECK_PYTHON
if not errorlevel 1 goto :RUN_PYTHON

py -3.12 -I -c "import sys,venv,ensurepip; sys.exit(sys.version_info[:2] < (3,10))" >nul 2>nul
if not errorlevel 1 goto :RUN_PY312
py -3 -I -c "import sys,venv,ensurepip; sys.exit(sys.version_info[:2] < (3,10))" >nul 2>nul
if not errorlevel 1 goto :RUN_PY3

for /f "delims=" %%P in ('where python.exe 2^>nul') do (
	set "TTS_CANDIDATE=%%P"
	call :CHECK_PATH_PYTHON
	if not errorlevel 1 goto :RUN_PYTHON
)

for %%V in (312 313 314 315 311 310) do (
	set "TTS_CANDIDATE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
	call :CHECK_PYTHON
	if not errorlevel 1 goto :RUN_PYTHON
	set "TTS_CANDIDATE=%ProgramFiles%\Python%%V\python.exe"
	call :CHECK_PYTHON
	if not errorlevel 1 goto :RUN_PYTHON
	set "TTS_CANDIDATE=%SystemDrive%\Python%%V\python.exe"
	call :CHECK_PYTHON
	if not errorlevel 1 goto :RUN_PYTHON
)

echo Python 3.10 or newer was not found. Python 3.12 is recommended.
echo To select an existing Python, run these commands in PowerShell:
echo   $env:TTS_PYTHON = 'C:\Python312\python.exe'
echo   .\setup_env.cmd
exit /b 1

:CUSTOM_PYTHON
set "TTS_CANDIDATE=%TTS_PYTHON%"
call :CHECK_PYTHON
if not errorlevel 1 goto :RUN_PYTHON
echo TTS_PYTHON is not usable. Python 3.10+, venv and ensurepip are required.
exit /b 1

:CHECK_PATH_PYTHON
if /i not "%TTS_CANDIDATE:\Microsoft\WindowsApps\=%"=="%TTS_CANDIDATE%" exit /b 1
goto :CHECK_PYTHON

:CHECK_PYTHON
if not exist "%TTS_CANDIDATE%" exit /b 1
"%TTS_CANDIDATE%" -I -c "import sys,venv,ensurepip; sys.exit(sys.version_info[:2] < (3,10))" >nul 2>nul
if errorlevel 1 exit /b 1
set "TTS_BOOT_PY=%TTS_CANDIDATE%"
exit /b 0

:RUN_PYTHON
"%TTS_BOOT_PY%" -I "%~dp0setup_venv.py" %*
exit /b %errorlevel%

:RUN_PY312
py -3.12 -I "%~dp0setup_venv.py" %*
exit /b %errorlevel%

:RUN_PY3
py -3 -I "%~dp0setup_venv.py" %*
exit /b %errorlevel%
