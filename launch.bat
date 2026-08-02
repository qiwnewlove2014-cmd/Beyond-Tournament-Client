@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Beyond Tournament - Source Client

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

echo Starting Beyond Tournament Client with %PYTHON_EXE%...
"%PYTHON_EXE%" beyond_tournament.py
if errorlevel 1 (
    echo.
    echo [FAILED] The source client stopped with an error.
    echo Run install_libs.bat to repair and verify the development environment.
)
pause
