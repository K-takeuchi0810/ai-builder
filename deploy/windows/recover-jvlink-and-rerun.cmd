@echo off
setlocal
set "SCRIPT=%~dp0recover-jvlink-and-rerun.ps1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','""%SCRIPT%""'"
exit /b %ERRORLEVEL%
