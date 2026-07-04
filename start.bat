@echo off
REM ==========================================================================
REM  Polymarket Forecast Lab - one-button launcher
REM  Double-click this file to start everything:
REM    * Watchdog (this window): supervises the orchestrator, auto-restarts it
REM      10 minutes after any crash (config.yaml watchdog.restart_delay_seconds)
REM    * Dashboard (separate window): http://localhost:8501
REM  Close either window or press Ctrl+C in this window to stop everything.
REM ==========================================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] Virtual environment not found at .venv
  echo         Set it up first:  pip install uv  ^&^&  uv sync --all-groups
  echo.
  pause
  exit /b 1
)

echo ==========================================================================
echo  Polymarket Forecast Lab
echo    Dashboard : http://localhost:8501  (opens in a separate window)
echo    Watchdog  : this window  (auto-restarts on crash, Ctrl+C to stop)
echo ==========================================================================
echo.

REM Launch the dashboard in its own window (non-blocking).
start "Forecast Lab - Dashboard" "%PY%" -m streamlit run "src\lab\dashboard.py" --server.port 8501 --server.headless true

REM Run the watchdog in this window (blocks until stopped); it supervises
REM `lab run` and auto-restarts it 10 minutes after any exit.
"%PY%" -m lab watchdog

echo.
echo Watchdog stopped.
pause
