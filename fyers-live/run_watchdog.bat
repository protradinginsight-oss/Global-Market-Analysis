@echo off
REM Feed watchdog. Run in a SECOND window alongside run_collector.bat.
REM Detects shards that are alive but receiving nothing, and restarts them.
cd /d "%~dp0"
py -3.12 watchdog.py
echo.
pause
