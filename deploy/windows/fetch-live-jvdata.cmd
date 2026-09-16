@echo off
setlocal
set "ROOT=%~dp0..\.."
set "PY32=%ROOT%\..\keiba-yosou\.venv32\Scripts\python.exe"
if not exist "%PY32%" exit /b 2
cd /d "%ROOT%"
"%PY32%" "%ROOT%\deploy\windows\fetch-live-jvdata.py"
if not "%ERRORLEVEL%"=="0" exit /b %ERRORLEVEL%

rem During the final 15 minutes, run a second lightweight refresh after 30 seconds.
if exist "%ROOT%\out\cache\live-critical-window.flag" (
  powershell.exe -NoProfile -Command "Start-Sleep -Seconds 30"
  "%PY32%" "%ROOT%\deploy\windows\fetch-live-jvdata.py" --critical
)
exit /b %ERRORLEVEL%
