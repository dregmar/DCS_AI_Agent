@echo off
rem ============================================================================
rem  DCS George AI launcher (6 Nov 2026). Double-click it, or use the desktop
rem  shortcut "DCS George AI" (make it once with make_shortcut.bat).
rem   1. Runs start_george_ollama.ps1: stops any Ollama that isn't pinned, starts
rem      it on the RX 5700 XT (its GPU settings stay inside that script), loads
rem      qwen3:8b and says which GPU it reports. "launcher.bat skipollama" skips it.
rem   2. Runs main.py from this folder at below-normal priority, in this window,
rem      so the PTT / speech / latency lines stay visible.
rem ============================================================================
setlocal
cd /d "%~dp0"
title DCS George AI

rem --- Python: the project's own venv if there is one, else the py launcher, else python
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY set "PY=python"

rem --- Ollama, pinned to the RX 5700 XT
if /i "%~1"=="skipollama" goto ollama_done
if not exist "start_george_ollama.ps1" (
    echo start_george_ollama.ps1 isn't in this folder - start Ollama yourself.
    goto ollama_done
)
echo Starting Ollama for George ...
powershell -NoProfile -ExecutionPolicy Bypass -File "start_george_ollama.ps1"
if errorlevel 1 (
    echo.
    echo Ollama didn't start cleanly ^(see above^). Carrying on: the fast path works without it.
    echo.
)
:ollama_done

echo Starting DCS George AI with %PY% ...
echo.
start "DCS George AI" /belownormal /b /wait %PY% main.py
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" (
    echo DCS George AI stopped with code %RC%. The details are above and in the logs folder.
) else (
    echo DCS George AI has stopped.
)
echo Press any key to close this window.
pause >nul
endlocal
