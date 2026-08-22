@echo off
REM %~dp0 = this file's own folder, so the whole project can be moved
REM to another path or drive without editing anything here.
cd /d "%~dp0"
py -3.12 global_premarket.py
