@echo off
REM Live system health. Run this in a spare window during market hours to
REM watch data arriving - the "New" column shows rows added since the last
REM refresh, which is the difference between data being present and data
REM being collected.
cd /d "%~dp0"
py -3.12 healthcheck.py --watch
