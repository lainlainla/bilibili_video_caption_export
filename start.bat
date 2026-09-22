@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1

if not exist ".venv\Scripts\python.exe" (
    call "%~dp0install.bat"
    exit /b
)

set "PYTHONUTF8=1"
".venv\Scripts\python.exe" "start.py"
if errorlevel 1 (
    echo Launch failed. See the error above.
    pause
    exit /b 1
)
endlocal
