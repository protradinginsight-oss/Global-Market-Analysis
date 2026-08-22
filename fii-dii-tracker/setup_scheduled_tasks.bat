@echo off
REM Run this once, from an ordinary Command Prompt (no admin needed), to set up
REM the three scheduled tasks:
REM   - 7:30 PM daily
REM   - 9:00 PM daily  (NSE's provisional data timing varies ~6:30-8 PM IST;
REM                      the script skips work if today's data is already
REM                      stored, so this second run is a safe no-op if the
REM                      first one already succeeded)
REM   - At logon        (the "auto-resume when turned on" behavior - if the
REM                      PC was off at a scheduled time, this catches up as
REM                      soon as you log back in)
REM
REM Assumes this project lives at E:\Global Trading Analysis\fii-dii-tracker.
REM If that's not where it is, edit the path below to match.

set TRACKER_DIR=E:\Global Trading Analysis\fii-dii-tracker

schtasks /create /tn "FII_DII_Tracker_730PM" /tr "\"%TRACKER_DIR%\run_tracker.bat\"" /sc daily /st 19:30 /f
schtasks /create /tn "FII_DII_Tracker_900PM" /tr "\"%TRACKER_DIR%\run_tracker.bat\"" /sc daily /st 21:00 /f
schtasks /create /tn "FII_DII_Tracker_OnLogon" /tr "\"%TRACKER_DIR%\run_tracker.bat\"" /sc onlogon /f

echo.
echo Done. Verify with:
echo   schtasks /query /tn "FII_DII_Tracker_730PM"
echo   schtasks /query /tn "FII_DII_Tracker_900PM"
echo   schtasks /query /tn "FII_DII_Tracker_OnLogon"
echo.
echo To remove a task later: schtasks /delete /tn "FII_DII_Tracker_730PM" /f
