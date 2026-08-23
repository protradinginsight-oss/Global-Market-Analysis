@echo off
REM ===========================================================================
REM  Schedule the components that currently rely on you remembering them.
REM
REM  Already scheduled elsewhere:
REM    FII/DII tracker    7:30 PM and 9:00 PM
REM    Global pre-market  8:15 AM and 9:00 AM
REM
REM  This adds:
REM    events --fetch        8:00 AM daily  (calendar rolls forward daily)
REM    dashboard build       8:20 AM daily  (ready before the open)
REM    universe snapshot     1st of month   (survivorship-bias record)
REM    MCX rebuild           Sunday 6 PM    (contracts expire constantly)
REM
REM  NOT scheduled, deliberately:
REM    token refresh   - needs four interactive browser logins
REM    live collector  - you want to see it start and watch it run
REM    option chains   - same, and it should not run unattended before
REM                      the tick collector is confirmed working
REM
REM  Run once from an ordinary Command Prompt. Assumes the project sits at
REM  E:\Global Trading Analysis - edit ROOT below if not.
REM ===========================================================================

set ROOT=E:\Global Trading Analysis

echo Scheduling daily tasks...
echo.

schtasks /create /tn "Trading_Events_Fetch" ^
  /tr "cmd /c cd /d \"%ROOT%\events\" ^&^& py -3.12 events.py --fetch" ^
  /sc daily /st 08:00 /f

schtasks /create /tn "Trading_Dashboard_Build" ^
  /tr "cmd /c cd /d \"%ROOT%\dashboard\" ^&^& py -3.12 dashboard.py --no-open" ^
  /sc daily /st 08:20 /f

schtasks /create /tn "Trading_Universe_Snapshot" ^
  /tr "cmd /c cd /d \"%ROOT%\fyers-live\" ^&^& py -3.12 universe_snapshot.py" ^
  /sc monthly /d 1 /st 18:00 /f

schtasks /create /tn "Trading_MCX_Rebuild" ^
  /tr "cmd /c cd /d \"%ROOT%\fyers-live\" ^&^& py -3.12 mcx_setup.py --explore ^&^& py -3.12 mcx_setup.py --build ^&^& py -3.12 merge_universe.py" ^
  /sc weekly /d SUN /st 18:00 /f

echo.
echo ===========================================================================
echo  Done. Verify with:
echo    schtasks /query /tn "Trading_Events_Fetch"
echo    schtasks /query /tn "Trading_Dashboard_Build"
echo    schtasks /query /tn "Trading_Universe_Snapshot"
echo    schtasks /query /tn "Trading_MCX_Rebuild"
echo.
echo  Remove one later:
echo    schtasks /delete /tn "Trading_Events_Fetch" /f
echo ===========================================================================
echo.
echo  Still manual each morning, by design:
echo    refresh_tokens.bat   four browser logins, cannot be automated
echo    run_collector.bat    watch it start
echo    run_watchdog.bat
echo    run_chains.bat
echo    check.bat            watch the New column climb
echo.
pause
