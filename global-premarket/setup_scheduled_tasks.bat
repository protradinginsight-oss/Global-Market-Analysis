@echo off
REM Run once from an ordinary Command Prompt to schedule the pre-market pull.
REM
REM 8:15 AM IST - roughly an hour before the Indian open, by which point the
REM US session has closed and overnight moves are settled. Early enough to
REM act on, late enough to be meaningful.
REM
REM A second run at 9:00 AM catches anything that moved in the final hour.
REM
REM Assumes E:\Global Trading Analysis\global-premarket - edit if elsewhere.

set PM_DIR=E:\Global Trading Analysis\global-premarket

schtasks /create /tn "Global_Premarket_815AM" /tr "\"%PM_DIR%\run_premarket.bat\"" /sc daily /st 08:15 /f
schtasks /create /tn "Global_Premarket_900AM" /tr "\"%PM_DIR%\run_premarket.bat\"" /sc daily /st 09:00 /f

echo.
echo Done. Verify with:
echo   schtasks /query /tn "Global_Premarket_815AM"
echo   schtasks /query /tn "Global_Premarket_900AM"
echo.
echo To remove: schtasks /delete /tn "Global_Premarket_815AM" /f
