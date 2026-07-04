@echo off
if exist "Beyond Tournament\" (
    rmdir /s /q "Beyond Tournament"
    )
if not exist "Beyond Tournament\" (
    md "Beyond Tournament"
    md "Beyond Tournament\data"
    )
echo building...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-disable-console --windows-force-stderr=%program%final_hour.log --windows-force-stdout=%program%final_hour.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage final_hour.py
xcopy /S /Q  dlls_windows\* "Beyond Tournament\"
copy *.mhr "Beyond Tournament\"
copy default_keyconfig.json "Beyond Tournament\"
copy *.dll "Beyond Tournament\"
copy ffmpeg.exe "Beyond Tournament\"
copy oalinst.exe "Beyond Tournament\"
xcopy /E /I /Q final_hour.dist "Beyond Tournament"
ren "Beyond Tournament\final_hour.exe" "Beyond Tournament.exe"
echo build completed...
echo copying required data...
xcopy /E /I /Q data "Beyond Tournament\data\"
xcopy /E /I /Q urlextract "Beyond Tournament\urlextract\"
FOR /F "tokens=*" %%g IN ('python -c "import yt_dlp, os; print(os.path.dirname(yt_dlp.__file__))"') do (SET YT_DLP_PATH=%%g)
xcopy /E /I /Q "%YT_DLP_PATH%" "Beyond Tournament\yt_dlp"
if exist final_hour.dist\ (
    rmdir /s /q final_hour.dist
    )
echo build complete!