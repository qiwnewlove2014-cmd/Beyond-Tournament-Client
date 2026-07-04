@echo off
title final-hour terminal output
echo Starting Beyond Tournament Client...
python final_hour.py
if errorlevel 1 (
    echo.
    echo Launch failed. Trying with pipenv...
    python -m pipenv run python final_hour.py
)
pause
