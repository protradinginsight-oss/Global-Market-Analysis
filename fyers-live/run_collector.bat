@echo off
REM Live collector. Run during market hours (9:15 AM - 3:30 PM IST).
REM Requires fresh tokens - run refresh_tokens.bat first each morning.
cd /d "%~dp0"
py -3.12 collector.py
echo.
pause
