@echo off
rem snapshot.bat - save a snapshot of the project (run after each good test).
rem   Double-click: snapshot named with the date and time.
rem   From a command prompt:  snapshot "engage by voice works"
rem It first copies your live DCS scripts from Saved Games into dcs_scripts\ so they are
rem versioned together with the Python side.
setlocal
cd /d "%~dp0"
set "DCS_SCRIPTS=%USERPROFILE%\Saved Games\DCS\Scripts"
if not exist "dcs_scripts" mkdir "dcs_scripts"
for %%F in (Export.lua george_export.lua george_ai_export.lua) do (
  if exist "%DCS_SCRIPTS%\%%F" copy /y "%DCS_SCRIPTS%\%%F" "dcs_scripts\%%F" >nul
)
set "MSG=%~1"
if "%MSG%"=="" set "MSG=Snapshot %DATE% %TIME%"
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "%MSG%"
  echo Saved snapshot: %MSG%
) else (
  echo Nothing has changed since the last snapshot.
)
echo.
echo Last snapshots:
git log --oneline -5
if "%~1"=="" pause
endlocal
