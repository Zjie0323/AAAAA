@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  A-stock realtime board starter (safe edition)
REM  IMPORTANT: on stripped CN CMD consoles (GBK, no timeout/chcp),
REM  directly launching a SHORT-LIVED console exe in the foreground
REM  (e.g. python -c, powershell probe, ping) closes the cmd window.
REM  Therefore every external tool below runs inside for/f or pipes
REM  (child processes). Only the long-running server.py runs in the
REM  foreground - that is safe.
REM ============================================================

set "PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%"

set "DIAG=%~dp0_start.log"
echo =================== %date% %time% =================== > "%DIAG%"
echo [diag] CD=%CD% >> "%DIAG%"
echo [diag] SystemRoot=%SystemRoot% >> "%DIAG%"
echo [diag] PATH=%PATH% >> "%DIAG%"

set "PYEXE="
if defined PYTHON if exist "%PYTHON%" set "PYEXE=%PYTHON%"
if defined WORKBUDDY_PYTHON if exist "%WORKBUDDY_PYTHON%" set "PYEXE=%WORKBUDDY_PYTHON%"
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
)
if not defined PYEXE (
  for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
)
if not defined PYEXE (
  for %%V in (313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
      if not defined PYEXE set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    )
  )
)
if not defined PYEXE (
  echo [diag] FATAL: no python found >> "%DIAG%"
  echo.
  echo [ERROR] Python interpreter not found.
  echo Install Python 3.7+ with "Add python.exe to PATH" checked:
  echo   https://www.python.org/downloads/
  echo Or set env var before running:
  echo   set PYTHON=C:\path\to\python.exe
  echo.
  pause
  exit /b 1
)
echo [diag] PYEXE=%PYEXE% >> "%DIAG%"

REM Warn if Microsoft Store alias (unreliable) - pipe findstr is a child, safe
echo "%PYEXE%" | findstr /I "\WindowsApps\python.exe" >nul
if not errorlevel 1 (
  echo [diag] WARNING: store-alias-detected >> "%DIAG%"
  echo.
  echo [WARNING] Microsoft Store Python alias detected: %PYEXE%
  echo It may fail silently. If server does not start, install Python
  echo from https://www.python.org/downloads/ instead.
  echo.
)

set PORT=8000
if not "%~1"=="" set PORT=%~1

REM Clean stale listeners on port (all inside for/f child processes)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%P >nul 2>&1
)

echo Using Python: %PYEXE%
echo Starting A-stock realtime board at http://localhost:%PORT%
echo (Press Ctrl+C to stop)
echo [diag] launching: "%PYEXE%" "%~dp0server.py" %PORT% >> "%DIAG%"
"%PYEXE%" "%~dp0server.py" %PORT%
echo [diag] server exited >> "%DIAG%"
pause
endlocal