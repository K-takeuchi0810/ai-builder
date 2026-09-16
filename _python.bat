@echo off
rem Resolve the Python interpreter into %MAIB_PY%. Called by the other .bat files.
rem
rem Order:
rem   1. %MAIB_PYTHON% if set (explicit override)
rem   2. ..\keiba-yosou\.venv64\Scripts\python.exe  (the normal case)
rem
rem The system "python" is deliberately NOT used: it is 32-bit and has no numpy,
rem so imports fail immediately, and even with numpy it cannot hold the
rem 425-column x 6-year matrix (about 5 GB).
rem
rem NOTE: keep this file ASCII-only. cmd.exe reads .bat as the console codepage
rem (cp932 here), so UTF-8 Japanese comments get mangled into stray commands.

if defined MAIB_PYTHON (
  if exist "%MAIB_PYTHON%" (
    set "MAIB_PY=%MAIB_PYTHON%"
    set "PYTHONIOENCODING=utf-8"
    exit /b 0
  )
  echo [error] MAIB_PYTHON does not point to an existing file: %MAIB_PYTHON%
  exit /b 1
)

set "MAIB_PY=%~dp0..\keiba-yosou\.venv64\Scripts\python.exe"
if not exist "%MAIB_PY%" (
  echo [error] Python not found: %MAIB_PY%
  echo         Put keiba-yosou next to this repo, or set MAIB_PYTHON:
  echo             set MAIB_PYTHON=C:\path\to\python.exe
  exit /b 1
)

rem Keep Japanese output readable even when the console is cp932
set "PYTHONIOENCODING=utf-8"
exit /b 0
