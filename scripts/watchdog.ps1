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
$LockDir   = Join-Path $Root "data\watchdog.lock"
$StartLock = Join-Path $Root "data\orchestrator.starting"

# Consider the process hung (not merely busy) if the heartbeat is older than this.
$HeartbeatStaleMinutes = 45
$StartupGraceSeconds   = 30

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $Log -Value "$ts  $msg"
}

function Enter-WatchdogLock {
    if (Test-Path $LockDir) {
        $age = (Get-Date) - (Get-Item $LockDir).LastWriteTime
        if ($age.TotalMinutes -lt 5) {
            Write-Log "watchdog already running (lock age $([int]$age.TotalSeconds)s) -- skip"
            exit 0
        }
        Remove-Item $LockDir -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $LockDir | Out-Null
}

function Exit-WatchdogLock {
    Remove-Item $LockDir -Force -ErrorAction SilentlyContinue
}

function Get-LabProcessPids([string]$Role) {
    $pattern = switch ($Role) {
        "orchestrator" { "-m\s+lab\s+run" }
        "dashboard"    { "streamlit.+dashboard" }
        default        { return @() }
    }
    $pids = @()
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.CommandLine -and $_.CommandLine -match $pattern) {
            $pids += $_.ProcessId
        }
    }
    return $pids
}

function Test-StartupInProgress {
    if (-not (Test-Path $StartLock)) { return $false }
    $age = (Get-Date) - (Get-Item $StartLock).LastWriteTime
    return ($age.TotalSeconds -lt $StartupGraceSeconds)
}

function Invoke-GuardCleanup {
    if (-not (Test-Path $Py)) { return }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Py -m lab guard 2>&1
        foreach ($line in @($output)) {
            Write-Log "guard: $line"
        }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Test-DashboardAlive {
    $hit = netstat -ano 2>$null | Select-String ":8501\s" | Select-String "LISTENING"
    return [bool]$hit
}

function Start-Dashboard {
    if (-not (Test-Path $Py)) {
        Write-Log "ERROR: venv python not found at $Py -- cannot start dashboard"
        return
    }
    if (Test-StartupInProgress) {
        Write-Log "dashboard startup in progress -- skip spawn"
        return
    }
    $existing = Get-LabProcessPids "dashboard"
    if ($existing.Count -gt 0) {
        Write-Log "dashboard process already running (pids=$($existing -join ',')) -- skip spawn"
        return
    }
    Set-Content -Path $StartLock -Value "dashboard" -Encoding ascii
    try {
        Start-Process -FilePath $Py `
            -ArgumentList @("-m", "streamlit", "run", "src\lab\dashboard.py",
                              "--server.port", "8501", "--server.headless", "true") `
            -WorkingDirectory $Root `
            -WindowStyle Hidden
        Write-Log "started dashboard (streamlit :8501)"
        Start-Sleep -Seconds 3
    } finally {
        Remove-Item $StartLock -Force -ErrorAction SilentlyContinue
    }
}

function Test-OrchestratorAlive {
    if (Test-StartupInProgress) {
        return $true
    }
    $procPids = Get-LabProcessPids "orchestrator"
    if ($procPids.Count -gt 0) {
        return $true
    }
    if (-not (Test-Path $PidFile)) { return $false }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $false }
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if ($proc.ProcessName -notlike "python*") { return $false }
    return $true
}

function Start-Orchestrator {
    if (-not (Test-Path $Py)) {
        Write-Log "ERROR: venv python not found at $Py -- cannot start"
        return
    }
    if (Test-StartupInProgress) {
        Write-Log "orchestrator startup already in progress -- skip spawn"
        return
    }
    $existing = Get-LabProcessPids "orchestrator"
    if ($existing.Count -gt 0) {
        Write-Log "orchestrator process already running (pids=$($existing -join ',')) -- skip spawn"
        return
    }
    Set-Content -Path $StartLock -Value "orchestrator" -Encoding ascii
    try {
        Start-Process -FilePath $Py `
            -ArgumentList @("-m", "lab", "run") `
            -WorkingDirectory $Root `
            -WindowStyle Hidden
        Write-Log "started orchestrator (lab run)"
        Start-Sleep -Seconds 3
    } finally {
        Remove-Item $StartLock -Force -ErrorAction SilentlyContinue
    }
}

Enter-WatchdogLock
try {
    Invoke-GuardCleanup

    if (Test-OrchestratorAlive) {
        $note = "healthy"
        if (Test-Path $Heartbeat) {
            $age = (Get-Date) - (Get-Item $Heartbeat).LastWriteTime
            if ($age.TotalMinutes -gt $HeartbeatStaleMinutes) {
                $note = "ALIVE but heartbeat stale ({0:N0} min) -- possible hang; not auto-killing" -f $age.TotalMinutes
            }
        }
        Write-Log $note
    } else {
        Write-Log "orchestrator not running -- starting"
        Start-Orchestrator
    }

    if (-not (Test-DashboardAlive)) {
        if (Test-StartupInProgress) {
            Write-Log "dashboard startup in progress -- waiting"
            Start-Sleep -Seconds 5
        }
        if (-not (Test-DashboardAlive)) {
            Write-Log "dashboard not running -- starting"
            Start-Dashboard
        } else {
            Write-Log "dashboard healthy after wait"
        }
    } else {
        Write-Log "dashboard healthy"
    }
} finally {
    Exit-WatchdogLock
}
exit 0
