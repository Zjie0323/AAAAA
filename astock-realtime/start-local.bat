@echo off
REM ============================================================
REM  Local starter (this machine only): uses WorkBuddy bundled Python.
REM  This path only exists on Admin's machine; others should use start.bat.
REM  All messages are ASCII English to stay clean on stripped CN CMDs.
REM ============================================================
set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYEXE%" (
    echo [ERROR] WorkBuddy Python not found: %PYEXE%
    echo Please use start.bat or install/configure system Python.
    pause
    exit /b 1
)
set PORT=8000
if not "%~1"=="" set PORT=%~1
echo [Local] %PYEXE%
echo Starting A-stock realtime board at http://localhost:%PORT%
echo (Press Ctrl+C to stop)
"%PYEXE%" "%~dp0server.py" %PORT%
pause