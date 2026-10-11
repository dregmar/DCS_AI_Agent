@echo off
rem setup_git.bat - run ONCE: turns this folder into a local Git repository and saves a first
rem snapshot. Nothing is uploaded anywhere; the history lives in the hidden .git folder here.
setlocal
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed. Install "Git for Windows" from https://git-scm.com/download/win
  echo then run this file again.
  pause
  exit /b 1
)
if exist ".git" (
  echo This folder is already a Git repository.
) else (
  git init -q -b main
  echo Created a Git repository in %CD%
)
rem Who the snapshots are by (only set for this repository, only if not set already).
git config user.name >nul 2>nul || git config user.name "Andre"
git config user.email >nul 2>nul || git config user.email "andre.gordon.m@gmail.com"
git config core.autocrlf true
call "%~dp0snapshot.bat" "Initial snapshot: DCS George AI middleware"
echo.
echo Done. After each good test, double-click snapshot.bat to save a new snapshot.
pause
endlocal
