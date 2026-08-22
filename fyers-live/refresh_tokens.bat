@echo off
REM Daily token refresh. Run this each morning before the market opens.
REM %~dp0 = this file's own folder, so the project can be moved freely.
cd /d "%~dp0"
py -3.12 token_manager.py
echo.
pause
