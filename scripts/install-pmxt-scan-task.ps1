# Registers a Windows Scheduled Task that runs the out-of-band pmxt Router
# scan (scripts\pmxt_router_scan.py) twice a day. This is deliberately a
# SEPARATE task from install-watchdog.ps1 -- pmxt is a third-party trading
# SDK (Claude.md tech-stack row / S12) whose hosted API key can also place
# live trades, so it must never run inside the lab's own orchestrator
# process or its own dependency tree. `uv run --with pmxt` installs pmxt
# into an ephemeral/cached environment for just this invocation --
# pyproject.toml is never touched.
#
# Run once:   powershell -ExecutionPolicy Bypass -File scripts\install-pmxt-scan-task.ps1
# Remove:     powershell -ExecutionPolicy Bypass -File scripts\uninstall-pmxt-scan-task.ps1
#
# The scan writes data\pmxt_candidates.json; lab.models.m7_crossvenue's own
# verify_pmxt_candidates (run twice daily inside the orchestrator, at
# cross_venue.pmxt_verify_cron in config.yaml, default 06:00/18:00 UTC)
# reads that file and applies our own independent LLM check before anything
# reaches data/markets_map.yaml's `proposed` list. This task's own local
# run times (05:00 / 17:00) are deliberately ~1h ahead of the UTC verify
# window so fresh candidates are usually ready by the time it runs --
# adjust if your local UTC offset makes that buffer too tight.

$ErrorActionPreference = "Stop"

$Root      = Split-Path -Parent $PSScriptRoot
$Uv        = (Get-Command uv -ErrorAction SilentlyContinue).Source
$TaskName  = "PolymarketForecastLabPmxtScan"
$ScriptPath = Join-Path $Root "scripts\pmxt_router_scan.py"

if (-not (Test-Path $ScriptPath)) {
    throw "pmxt_router_scan.py not found at $ScriptPath"
}
if (-not $Uv) {
    throw "uv not found on PATH -- install it first (this task needs `uv run --with pmxt`)."
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$action = New-ScheduledTaskAction `
    -Execute $Uv `
    -Argument "run --with pmxt python `"$ScriptPath`"" `
    -WorkingDirectory $Root

$morning = New-ScheduledTaskTrigger -Daily -At "05:00"
$evening = New-ScheduledTaskTrigger -Daily -At "17:00"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($morning, $evening) `
        -Settings $settings `
        -Principal $principal `
        -Description "Twice daily: out-of-band pmxt Router scan for M7 candidate cross-venue matches. Never run by the lab's own orchestrator." `
        -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName' (05:00 and 17:00 daily)."
} catch {
    Write-Warning "PowerShell Register-ScheduledTask failed: $_"
    Write-Host "Trying schtasks fallback (single daily trigger only; add the second manually via Task Scheduler if needed)..."
    $cmd = "`"$Uv`" run --with pmxt python `"$ScriptPath`""
    & schtasks.exe /Create /TN $TaskName /TR $cmd /SC DAILY /ST 05:00 /RL LIMITED /F
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Registered scheduled task '$TaskName' via schtasks (05:00 daily)."
    } else {
        Write-Warning "schtasks registration failed (exit $LASTEXITCODE)."
    }
}

Write-Host ""
Write-Host "NOTE: this is the FIRST real invocation of pmxt against the live API from"
Write-Host "this project. Its response field names were assembled from partial public"
Write-Host "docs and could not be tested in advance (see pmxt_router_scan.py's own"
Write-Host "docstring). Run it once by hand now to check for a 'pmxt schema mismatch'"
Write-Host "message before waiting for the first scheduled fire:"
Write-Host "  uv run --with pmxt python `"$ScriptPath`""
