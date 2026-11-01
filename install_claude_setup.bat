@echo off
rem install_claude_setup.bat - run ONCE: copies claude_setup\ into .claude\ (Claude Code's
rem project settings and hooks), then removes claude_setup\. Safe to run again.
setlocal
cd /d "%~dp0"
if not exist "claude_setup\settings.json" (
  echo claude_setup\settings.json not found - nothing to install.
  pause
  exit /b 1
)
if exist ".claude\settings.json" (
  echo .claude\settings.json already exists - keeping a copy as .claude\settings.json.bak
  copy /y ".claude\settings.json" ".claude\settings.json.bak" >nul
)
xcopy /e /i /y /q "claude_setup" ".claude" >nul
if errorlevel 1 (
  echo Copy failed.
  pause
  exit /b 1
)
rmdir /s /q "claude_setup"
echo Installed: .claude\settings.json, .claude\hooks\, .claude\tools\
where python >nul 2>nul || echo WARNING: "python" is not on PATH - the hooks need it.
where git >nul 2>nul || echo WARNING: Git for Windows not found - install it so Claude Code has bash.
pause
endlocal
