@echo off
set PreferredToolArchitecture=x64
echo Compiling test_yt_dlp.py with PreferredToolArchitecture=x64 ...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory test_yt_dlp.py
if errorlevel 1 (
    echo Compilation failed!
    exit /b 1
)
echo Compilation succeeded!
test_yt_dlp.dist\test_yt_dlp.exe
