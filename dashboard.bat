@echo off
REM ==========================================================================
REM  Polymarket Forecast Lab - dashboard launcher
REM  Double-click to open the dashboard in your browser (http://localhost:8501).
REM  Starts the Streamlit server first if it is not already running.
REM  This does NOT start data collection / analytics -- use start.bat for that.
REM ==========================================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "URL=http://localhost:8501"

if not exist "%PY%" (
  echo [ERROR] Virtual environment not found at .venv
  echo         Set it up first:  pip install uv  ^&^&  uv sync --all-groups
  echo.
  pause
  exit /b 1
)

REM Start the server only if nothing is already listening on port 8501.
netstat -ano | findstr ":8501" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo Starting dashboard server...
  start "Forecast Lab - Dashboard" "%PY%" -m streamlit run "src\lab\dashboard.py" --server.port 8501 --server.headless true
  echo Waiting for the server to come up...
  timeout /t 5 /nobreak >nul
) else (
  echo Dashboard server already running on port 8501.
)

echo Opening %URL% ...
start "" "%URL%"
