@echo off
rem Build today's base columns. This is the one morning preparation step.
rem
rem   build_daily.bat                    today
rem   build_daily.bat --date 20260802    a specific day
rem   build_daily.bat --rebuild          rebuild an existing cache
rem
rem Takes about 5 minutes for a 36-race day. Run it in the morning so the
rem server starts instantly later.
rem
rem NOTE: keep this file ASCII-only (see _python.bat).

setlocal
call "%~dp0_python.bat" || exit /b 1
"%MAIB_PY%" "%~dp0scripts_build_daily.py" %*
endlocal
