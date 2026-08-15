@echo off
if not exist "build_server_config.json" if not defined BT_SERVER_HOST (
    copy /Y "build_server_config.example.json" "build_server_config.json" >nul
    echo ERROR: build_server_config.json was created. Open it, enter the official hostname, and run build.bat again.
    exit /b 1
)
echo packing data...
python tools\pack_data.py
if errorlevel 1 exit /b 1
echo building...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-disable-console --windows-force-stderr=%program%Beyond_Tournament.log --windows-force-stdout=%program%Beyond_Tournament.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage beyond_tournament.py
if errorlevel 1 exit /b 1
if exist "Beyond Tournament\" (
    rmdir /s /q "Beyond Tournament"
    )
if not exist "Beyond Tournament\" (
    md "Beyond Tournament"
    )
xcopy /S /Q  dlls_windows\* "Beyond Tournament\"
copy *.mhr "Beyond Tournament\"
copy default_keyconfig.json "Beyond Tournament\"
copy "..\server\changelog.txt" "Beyond Tournament\"
copy *.dll "Beyond Tournament\"
copy ffmpeg.exe "Beyond Tournament\"
copy ffmpeg$.exe "Beyond Tournament\"
copy oalinst.exe "Beyond Tournament\"
xcopy /E /I /Q beyond_tournament.dist "Beyond Tournament"
ren "Beyond Tournament\beyond_tournament.exe" "Beyond Tournament.exe"
echo build completed...
echo copying required data...
copy sounds.dat "Beyond Tournament\"
del sounds.dat
xcopy /E /I /Q urlextract "Beyond Tournament\urlextract\"
FOR /F "tokens=*" %%g IN ('python -c "import yt_dlp, os; print(os.path.dirname(yt_dlp.__file__))"') do (SET YT_DLP_PATH=%%g)
xcopy /E /I /Q "%YT_DLP_PATH%" "Beyond Tournament\yt_dlp"
if exist beyond_tournament.dist\ (
    rmdir /s /q beyond_tournament.dist
    )
echo build complete!
