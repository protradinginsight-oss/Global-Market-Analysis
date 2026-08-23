@echo off
REM Morning brief - one read across every component.
REM Run after the pre-market and chain collectors have gone.
cd /d "%~dp0"
py -3.12 brief.py
echo.
pause
