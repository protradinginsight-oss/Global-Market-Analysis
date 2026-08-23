@echo off
REM Option chain collector. Run during market hours alongside the tick
REM collector - different data, different mechanism, no conflict. Stops
REM itself at the close.
cd /d "%~dp0"
py -3.12 option_chain.py
echo.
pause
