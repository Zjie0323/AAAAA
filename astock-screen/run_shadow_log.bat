@echo off
REM ============================================================
REM  Shadow log: smart-recommend ranking shadow record (daily).
REM  Entry point for Windows Task Scheduler (weekdays 15:10).
REM  Records today's picks for every ranking variant, then
REM  settles the previous trading day's real outcome.
REM  Must NOT contain pause - it runs non-interactively.
REM  Real logic lives in shadow_log.py; this is only a launcher.
REM ============================================================
setlocal
cd /d "%~dp0"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%"

set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
)
if not exist "%PYEXE%" (
  echo [ERROR] Python interpreter not found.
  exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%PYEXE%" "%~dp0shadow_log.py" daily
exit /b %ERRORLEVEL%
