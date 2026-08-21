@echo off
rem ============================================================
rem  Beyond Tournament - Debug Memory Allocator Launcher
rem ============================================================
rem  Runs the client with PYTHONMALLOC=debug: every heap
rem  allocation gets guard bytes. If any native library writes
rem  out of bounds, Python reports "bad leading/trailing pad
rem  byte" or crashes AT THE WRITE SITE (faulthandler captures
rem  it in native_crash.log) instead of silently corrupting the
rem  heap for the garbage collector to trip over later.
rem
rem  The game will run somewhat slower in this mode - normal.
rem  Use the normal launcher for regular play; use this one to
rem  hunt the crash.
rem
rem  Output files (send both to the developer after a crash):
rem    native_crash.log         - native crash thread stacks
rem    malloc_debug_output.log  - heap corruption reports
rem ============================================================
cd /d "%~dp0"
set PYTHONMALLOC=debug
set PYTHONTRACEMALLOC=3
echo Starting Beyond Tournament with PYTHONMALLOC=debug ...
python beyond_tournament.py > malloc_debug_output.log 2>&1
echo.
echo Game exited. Diagnostics saved to:
echo   native_crash.log          (native crash stacks)
echo   malloc_debug_output.log   (heap corruption reports)
echo.
pause
