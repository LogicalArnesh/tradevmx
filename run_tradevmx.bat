@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [TradeVMx] Creating virtual environment...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [TradeVMx] Python 3.11+ is required.
        echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 goto :fail
    "%PY%" -m pip install --quiet --upgrade pip
)

"%PY%" -c "import flask, webview, reportlab, yfinance, telegram, apscheduler, groq" >nul 2>nul
if errorlevel 1 (
    echo [TradeVMx] Installing dependencies ^(first run only^)...
    "%PY%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :fail
)

echo [TradeVMx] Starting the desk...
"%PY%" desktop.py %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo [TradeVMx] Startup failed. Read the messages above, then try again.
pause
exit /b 1
