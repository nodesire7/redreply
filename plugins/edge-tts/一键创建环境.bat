@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title TTS CLI Setup
call "%~dp0setup_env.cmd"
set "TTS_SETUP_EXIT=%errorlevel%"
echo.
if "%TTS_SETUP_EXIT%"=="0" goto :SETUP_OK
echo [ERROR] Setup did not finish. See the messages above.
goto :SETUP_DONE

:SETUP_OK
echo [OK] Setup completed. Use tts.cmd to run the tool.

:SETUP_DONE
echo Press any key to close this window...
pause >nul
exit /b %TTS_SETUP_EXIT%
