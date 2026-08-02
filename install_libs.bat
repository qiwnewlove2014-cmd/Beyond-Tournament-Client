@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Beyond Tournament - Development Setup
color 0A

echo ========================================================
echo Beyond Tournament - Automatic Development Setup
echo ========================================================
echo This window will report every setup step.
echo Existing system Python installations will not be removed.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_dev_environment.ps1" %*
set "SETUP_EXIT=%ERRORLEVEL%"

echo.
if "%SETUP_EXIT%"=="0" (
    echo [SUCCESS] Development setup completed.
) else (
    echo [FAILED] Development setup stopped with exit code %SETUP_EXIT%.
    echo Read the error above or open the newest file in setup_logs.
)

echo.
echo %* | findstr /I /C:"-NoPause" >nul
if errorlevel 1 pause
exit /b %SETUP_EXIT%
