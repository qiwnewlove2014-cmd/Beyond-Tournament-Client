@echo off
rem ============================================================
rem  Diagnostic launcher: anti-cheat PROCESS SCAN disabled
rem ============================================================
rem  Sets BT_DISABLE_ANTICHEAT_SCAN=1 so the psutil/Win32 process
rem  scan is skipped every second (speedhack check still runs).
rem  Used to bisect the CRT-heap corruption crash: if the game
rem  survives the water-zombie map with this launcher, psutil is
rem  the corrupter and the scan will be moved to a subprocess.
rem ============================================================
cd /d "%~dp0"
set BT_DISABLE_ANTICHEAT_SCAN=1
echo Starting Beyond Tournament with anti-cheat process scan DISABLED (diagnostic) ...
python beyond_tournament.py
echo.
echo Game exited.
pause
