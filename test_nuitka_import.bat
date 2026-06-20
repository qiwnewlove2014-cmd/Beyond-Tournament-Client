@echo off
echo Compiling test_nuitka_import.py...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage test_nuitka_import.py

if errorlevel 1 (
    echo Compilation failed!
    exit /b 1
)

echo Copying yt_dlp package to test_nuitka_import.dist...
xcopy /E /I /Q "C:\Users\Mongkol\AppData\Local\Programs\Python\Python313\Lib\site-packages\yt_dlp" "test_nuitka_import.dist\yt_dlp"

echo Running test_nuitka_import.exe...
test_nuitka_import.dist\test_nuitka_import.exe
