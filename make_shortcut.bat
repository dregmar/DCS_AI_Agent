@echo off
rem Makes (or remakes) the desktop shortcut "DCS George AI" to launcher.bat, with george.ico.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\DCS George AI.lnk');" ^
  "$s.TargetPath = '%~dp0launcher.bat';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.IconLocation = '%~dp0george.ico';" ^
  "$s.Description = 'DCS George AI - crew, command roles and kneeboard';" ^
  "$s.Save()"
if errorlevel 1 (
    echo Couldn't make the shortcut.
) else (
    echo Desktop shortcut "DCS George AI" made.
)
pause
endlocal
