@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo [TradeVMx] Registering the desk to start at login...

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [TradeVMx] Run run_tradevmx.bat once first - it creates the environment.
    pause
    exit /b 1
)

schtasks /Create /TN "TradeVMx" /TR "\"%~dp0run_tradevmx.bat\" --no-browser" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (
    echo.
    echo [TradeVMx] Could not register the task. Run this file as your own user,
    echo            not as an administrator, then try again.
    pause
    exit /b 1
)

echo.
echo [TradeVMx] Done. TradeVMx now starts silently at every login and keeps
echo            sweeping the market and pushing alerts.
echo            Console:  http://127.0.0.1:8765
echo            Remove:   schtasks /Delete /TN TradeVMx /F
echo.
pause
