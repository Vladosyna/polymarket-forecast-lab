@echo off
REM ==========================================================================
REM  Polymarket Forecast Lab - one-button launcher
REM  Double-click this file to start everything:
REM    * Orchestrator (this window): data collection + scheduled analytics
REM    * Dashboard (separate window): http://localhost:8501
REM  Close either window or press Ctrl+C in this window to stop.
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
echo    Dashboard    : http://localhost:8501  (opens in a separate window)
echo    Orchestrator : this window  (Ctrl+C to stop)
echo ==========================================================================
echo.

REM Launch the dashboard in its own window (non-blocking).
start "Forecast Lab - Dashboard" "%PY%" -m streamlit run "src\lab\dashboard.py" --server.port 8501 --server.headless true

REM Run the orchestrator in this window (blocks until stopped).
"%PY%" -m lab run

echo.
echo Orchestrator stopped.
pause
