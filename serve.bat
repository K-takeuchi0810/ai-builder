@echo off
rem Start the MAIBuilder server, then open http://127.0.0.1:8780/ in a browser.
rem
rem   serve.bat                          today (run build_daily.bat first)
rem   serve.bat --date 20260802          a specific day
rem   serve.bat --build                  build the day's matrix now (waits ~5 min)
rem   serve.bat --port 8781              use another port
rem   serve.bat --no-backtest            skip the backtest matrix (fastest start)
rem
rem The date and the backtest range both default to sensible values, so no
rem arguments are needed for normal use. Stop the server with Ctrl+C.
rem
rem NOTE: keep this file ASCII-only (see _python.bat).

setlocal
call "%~dp0_python.bat" || exit /b 1
cd /d "%~dp0"
"%MAIB_PY%" -m builder.api %*
endlocal
