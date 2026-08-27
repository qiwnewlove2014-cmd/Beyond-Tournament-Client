@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
if not "%~2"=="" goto usage
if not "%~1"=="" if /I not "%~1"=="--check" goto usage
set "BT_VERIFIED_PYTHON="
for /f "delims=" %%P in ('%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoLogo -NoProfile -File "%~dp0tools\select_build_python.ps1"') do set "BT_VERIFIED_PYTHON=%%P"
if not defined BT_VERIFIED_PYTHON goto failed
echo checking build inputs...
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py preflight
if errorlevel 1 goto failed
if /I "%~1"=="--check" goto checked
if not exist "build_server_config.json" if not defined BT_SERVER_HOST (
    copy /Y "build_server_config.example.json" "build_server_config.json" >nul
    echo ERROR: build_server_config.json was created. Open it, enter the official hostname, and run build.bat again.
    goto failed
)
set "BT_PACKAGE_STAGE=Beyond Tournament.pending"
md "%BT_PACKAGE_STAGE%"
if errorlevel 1 goto failed
echo packing data...
"%BT_VERIFIED_PYTHON%" -I -X utf8 tools\pack_data.py --output "%BT_PACKAGE_STAGE%\sounds.dat"
if errorlevel 1 goto failed
echo building...
"%BT_VERIFIED_PYTHON%" -I -X utf8 -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-disable-console --windows-force-stderr=%program%Beyond_Tournament.log --windows-force-stdout=%program%Beyond_Tournament.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage beyond_tournament.py
if errorlevel 1 goto failed
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py compiled
if errorlevel 1 goto failed
echo copying required files, excluding dollar-sign names...
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py copy-inputs
if errorlevel 1 goto failed
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py copy-runtime
if errorlevel 1 goto failed
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py package
if errorlevel 1 goto failed
"%BT_VERIFIED_PYTHON%" -I -X utf8 -S tools\build_safety.py publish
if errorlevel 1 goto failed
echo build complete!
popd
exit /b 0

:checked
echo Read-only build check complete. Nothing was compiled or packaged.
popd
exit /b 0

:usage
echo Usage: build.bat [--check]
goto failed

:failed
echo BUILD FAILED. Do not distribute partial output. Review Beyond Tournament.pending if it exists.
if /I not "%BT_BUILD_NO_PAUSE%"=="1" (
    echo Review the error above. Press Enter to close this build window.
    set "BT_BUILD_ACK="
    set /p "BT_BUILD_ACK="
)
popd
exit /b 1
