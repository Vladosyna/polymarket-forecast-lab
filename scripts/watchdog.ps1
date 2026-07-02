# Polymarket Forecast Lab - watchdog.
# Ensures the orchestrator (`lab run`) is alive. If not, starts it detached.
# Intended to be run hourly by Windows Task Scheduler (see install-watchdog.ps1),
# but is safe to run by hand at any time.

$ErrorActionPreference = "Stop"

$Root      = Split-Path -Parent $PSScriptRoot
$Py        = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile   = Join-Path $Root "data\orchestrator.pid"
$Heartbeat = Join-Path $Root "data\orchestrator.heartbeat"
$LogDir    = Join-Path $Root "data\logs"
$Log       = Join-Path $LogDir "watchdog.log"

# Consider the process hung (not merely busy) if the heartbeat is older than this.
$HeartbeatStaleMinutes = 45

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $Log -Value "$ts  $msg"
}

function Test-OrchestratorAlive {
    if (-not (Test-Path $PidFile)) { return $false }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $false }
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    # Guard against PID reuse by an unrelated process.
    if ($proc.ProcessName -notlike "python*") { return $false }
    return $true
}

function Start-Orchestrator {
    if (-not (Test-Path $Py)) {
        Write-Log "ERROR: venv python not found at $Py -- cannot start"
        return
    }
    Start-Process -FilePath $Py `
        -ArgumentList @("-m", "lab", "run") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden
    Write-Log "started orchestrator (lab run)"
}

if (Test-OrchestratorAlive) {
    $note = "healthy"
    if (Test-Path $Heartbeat) {
        $age = (Get-Date) - (Get-Item $Heartbeat).LastWriteTime
        if ($age.TotalMinutes -gt $HeartbeatStaleMinutes) {
            $note = "ALIVE but heartbeat stale ({0:N0} min) -- possible hang; not auto-killing" -f $age.TotalMinutes
        }
    }
    Write-Log $note
    exit 0
}

Write-Log "orchestrator not running -- starting"
Start-Orchestrator
