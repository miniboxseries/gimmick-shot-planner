@echo off
REM Windows launcher - double-click me
cd /d "%~dp0"
python shot_planner.py serve
pause
