@echo off
rem Legado LAN APK share launcher.
rem Starts tools\apk_share\apk_share.py with Python 3.
rem Keep this file ASCII-only: cmd.exe reads .bat in the OEM code page.
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where python.exe >nul 2>&1 && set "PY=python"
if not defined PY (
    where py.exe >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo.
    echo [ERROR] Python 3 not found on PATH.
    echo         Install Python 3.x, or add python.exe to PATH.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0tools\apk_share\apk_share.py" %*
if errorlevel 1 (
    echo.
    pause
)
endlocal