@echo off
REM ============================================================
REM  Auto push: auction TOP3 -> WeChat subscribe message
REM  Entry point for Windows Task Scheduler (daily 09:25:40).
REM  Must NOT contain pause - it runs non-interactively.
REM  All messages are ASCII English to stay clean on stripped CN CMDs.
REM  Real logic lives in auto_push_bid.py; this is only a launcher.
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

"%PYEXE%" "%~dp0auto_push_bid.py" %*
exit /b %ERRORLEVEL%
