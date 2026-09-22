@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo Installation failed. See the message and .cache\install logs.
    pause
    exit /b 1
)
endlocal
