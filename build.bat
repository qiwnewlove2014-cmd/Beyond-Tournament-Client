@echo off
if exist "Beyond Tournament\" (
    rmdir /s /q "Beyond Tournament"
    )
if not exist "Beyond Tournament\" (
    md "Beyond Tournament"
    md "Beyond Tournament\data"
    )
echo building...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-disable-console --windows-force-stderr=%program%Beyond_Tournament.log --windows-force-stdout=%program%Beyond_Tournament.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage beyond_tournament.py
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
xcopy /E /I /Q data "Beyond Tournament\data\"
xcopy /E /I /Q urlextract "Beyond Tournament\urlextract\"
FOR /F "tokens=*" %%g IN ('python -c "import yt_dlp, os; print(os.path.dirname(yt_dlp.__file__))"') do (SET YT_DLP_PATH=%%g)
xcopy /E /I /Q "%YT_DLP_PATH%" "Beyond Tournament\yt_dlp"
if exist beyond_tournament.dist\ (
    rmdir /s /q beyond_tournament.dist
    )
echo build complete!