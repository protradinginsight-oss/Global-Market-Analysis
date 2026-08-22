@echo off
REM Runs the tracker using this file's own folder as the working directory -
REM %~dp0 means "wherever this .bat file itself is sitting," so this keeps
REM working even if the whole project folder gets moved or the drive letter
REM changes later.
cd /d "%~dp0"
py fii_dii_tracker.py
