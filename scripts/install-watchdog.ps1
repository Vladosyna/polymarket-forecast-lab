# Registers a Windows Scheduled Task that runs the watchdog every hour (and at
# logon). The watchdog starts the Forecast Lab orchestrator if it is not already
# running -- so the lab effectively stays up on its own.
#
# Run once:   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
# Remove:     powershell -ExecutionPolicy Bypass -File scripts\uninstall-watchdog.ps1

$ErrorActionPreference = "Stop"

$Root     = Split-Path -Parent $PSScriptRoot
$Watchdog = Join-Path $Root "scripts\watchdog.ps1"
$TaskName = "PolymarketForecastLabWatchdog"

if (-not (Test-Path $Watchdog)) {
    throw "watchdog.ps1 not found at $Watchdog"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`"" `
    -WorkingDirectory $Root

# Repeat hourly, indefinitely, starting one minute from now; plus at every logon.
$hourly = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$atLogon = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($hourly, $atLogon) `
    -Settings $settings `
    -RunLevel Limited `
    -Description "Hourly health check: starts the Polymarket Forecast Lab orchestrator if it is not running." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (hourly + at logon)."
Write-Host "Running the watchdog once now to start the lab if it is down..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Watchdog
Write-Host "Done. Check data\logs\watchdog.log for actions."
