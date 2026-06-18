@echo off
if exist final_hour\ (
    rmdir /s /q final_hour
    )
if not exist final_hour\ (
    md final_hour
    md final_hour\data
    )
echo building...
python -m nuitka --assume-yes-for-downloads --quiet --standalone --low-memory --python-flag=no_site --user-plugin=CyalPlugin.py --enable-plugin=tk-inter --windows-disable-console --windows-force-stderr=%program%final_hour.log --windows-force-stdout=%program%final_hour.log --include-package-data=certifi --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage final_hour.py
xcopy /S /Q  dlls_windows\* final_hour\
copy *.mhr final_hour\
copy default_keyconfig.json final_hour\
copy *.dll final_hour\
copy ffmpeg.exe final_hour\
copy oalinst.exe final_hour\
xcopy /E /I /Q final_hour.dist final_hour
echo build completed...
echo copying required data...
xcopy /E /I /Q data final_hour\data\
xcopy /E /I /Q urlextract final_hour\urlextract\
xcopy /E /I /Q "C:\Users\Mongkol\AppData\Local\Programs\Python\Python313\Lib\site-packages\yt_dlp" "final_hour\yt_dlp"
if exist final_hour.dist\ (
    rmdir /s /q final_hour.dist
    )
echo build complete!