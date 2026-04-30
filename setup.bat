@echo off
title Gemma 4 Heretic - One Click Setup
echo ==========================================
echo    Gemma 4 Heretic Agent - Setup
echo ==========================================
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Setup failed. Please check the messages above.
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo [SUCCESS] Setup complete! You can now run 'Launch_Gemma_Agent.bat'.
pause
