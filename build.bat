@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "SOURCE_DIR=%CD%"
set "BUILD_EXIT_CODE=1"
set "BUILD_STAGE="
set "IN_BUILD_STAGE="
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

echo ========================================================
echo Beyond Tournament Client Builder
echo Python: %PYTHON_EXE%
echo ========================================================

if /I "%~1"=="--check" (
    goto :check_only
)

"%PYTHON_EXE%" tools\verify_environment.py
if errorlevel 1 (
    echo [FAILED] The development environment is not ready.
    echo Run install_libs.bat, then try the build again.
    goto :cleanup
)

call :prepare_ascii_build_workspace
if errorlevel 1 goto :cleanup

echo building...
"%PYTHON_EXE%" -m nuitka --mingw64 --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-console-mode=disable --windows-force-stderr=%program%Beyond_Tournament.log --windows-force-stdout=%program%Beyond_Tournament.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage beyond_tournament.py
if errorlevel 1 (
    echo [FAILED] Nuitka compilation failed. No package was published.
    goto :cleanup
)

popd
set "IN_BUILD_STAGE="
cd /d "%SOURCE_DIR%"

if exist "Beyond Tournament\" rmdir /s /q "Beyond Tournament"
md "Beyond Tournament"
md "Beyond Tournament\data"

xcopy /S /Q dlls_windows\* "Beyond Tournament\"
copy *.mhr "Beyond Tournament\"
copy default_keyconfig.json "Beyond Tournament\"
copy "..\server\changelog.txt" "Beyond Tournament\"
copy *.dll "Beyond Tournament\"
copy ffmpeg.exe "Beyond Tournament\"
copy ffmpeg$.exe "Beyond Tournament\"
copy oalinst.exe "Beyond Tournament\"
xcopy /E /I /Q "%BUILD_STAGE%\beyond_tournament.dist" "Beyond Tournament"
ren "Beyond Tournament\beyond_tournament.exe" "Beyond Tournament.exe"
echo build completed...
echo copying required data...
xcopy /E /I /Q data "Beyond Tournament\data\"
xcopy /E /I /Q urlextract "Beyond Tournament\urlextract\"
echo validating compiled package...
"%PYTHON_EXE%" tools\finalize_client_package.py --output "Beyond Tournament"
if errorlevel 1 (
    echo [FAILED] The compiled package is incomplete. No package was published.
    goto :cleanup
)
echo build complete!
set "BUILD_EXIT_CODE=0"
goto :cleanup

:check_only
"%PYTHON_EXE%" tools\verify_environment.py
set "BUILD_EXIT_CODE=%ERRORLEVEL%"
goto :cleanup

:prepare_ascii_build_workspace
rem Nuitka's Windows dependency scanner resolves SUBST drives back to their
rem physical paths. Compile from a real ASCII-only directory instead, then copy
rem the finished distribution back to the source tree for normal packaging.
set "BUILD_STAGE_ROOT=%PUBLIC%\BeyondTournamentBuild"
if not exist "%BUILD_STAGE_ROOT%\" md "%BUILD_STAGE_ROOT%"
if errorlevel 1 (
    echo [FAILED] Could not create the public temporary build directory.
    exit /b 1
)

:choose_build_stage
set "BUILD_STAGE=%BUILD_STAGE_ROOT%\client-%RANDOM%-%RANDOM%"
if exist "%BUILD_STAGE%\" goto :choose_build_stage
md "%BUILD_STAGE%"
if errorlevel 1 (
    echo [FAILED] Could not create the temporary build workspace.
    exit /b 1
)

echo [INFO] Preparing Unicode-safe build workspace: %BUILD_STAGE%
robocopy "%SOURCE_DIR%" "%BUILD_STAGE%" /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS /NP /XD "%SOURCE_DIR%\.git" "%SOURCE_DIR%\.client_crash_sessions" "%SOURCE_DIR%\data" "%SOURCE_DIR%\dlls_windows" "%SOURCE_DIR%\Beyond Tournament" "%SOURCE_DIR%\beyond_tournament.build" "%SOURCE_DIR%\beyond_tournament.dist" "%SOURCE_DIR%\setup_logs" __pycache__ /XF settings.json keyconfig.json client_crash_state.json pending_crash_reports.json pending_crash_reports.lock *.log nuitka-crash-report.xml >nul
if errorlevel 8 (
    echo [FAILED] Could not copy the compiler workspace.
    exit /b 1
)

pushd "%BUILD_STAGE%" >nul
if errorlevel 1 (
    echo [FAILED] Could not enter the temporary build workspace.
    exit /b 1
)
set "IN_BUILD_STAGE=1"
set "PYTHON_EXE=.venv\Scripts\python.exe"
echo [INFO] Native DLL detection path: %BUILD_STAGE%
exit /b 0

:cleanup
if defined IN_BUILD_STAGE (
    popd
    set "IN_BUILD_STAGE="
)
cd /d "%SOURCE_DIR%"
if defined BUILD_STAGE if exist "%BUILD_STAGE%\" (
    echo "%BUILD_STAGE%" | findstr /B /I /L /C:"%PUBLIC%\BeyondTournamentBuild\client-" >nul
    if not errorlevel 1 (
        rmdir /s /q "%BUILD_STAGE%"
    ) else (
        echo [WARNING] Temporary build path failed its safety check and was not removed: %BUILD_STAGE%
    )
)
exit /b %BUILD_EXIT_CODE%
