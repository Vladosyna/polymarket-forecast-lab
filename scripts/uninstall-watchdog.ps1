# Removes the Forecast Lab watchdog scheduled task.
# Run: powershell -ExecutionPolicy Bypass -File scripts\uninstall-watchdog.ps1

$ErrorActionPreference = "Stop"
$TaskName = "PolymarketForecastLabWatchdog"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    Write-Host "Scheduled task '$TaskName' not found (nothing to remove)."
}
