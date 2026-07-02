# Removes the Forecast Lab watchdog scheduled task.
# Run: powershell -ExecutionPolicy Bypass -File scripts\uninstall-watchdog.ps1

$ErrorActionPreference = "Stop"
$TaskName = "PolymarketForecastLabWatchdog"
$HourlyTaskName = "PolymarketForecastLabWatchdogHourly"

foreach ($name in @($TaskName, $HourlyTaskName)) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed scheduled task '$name'."
    }
}
$removed = $false
foreach ($name in @($TaskName, $HourlyTaskName)) {
    & schtasks.exe /Delete /TN $name /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removed scheduled task '$name' (schtasks)."
        $removed = $true
    }
}
if (-not $removed) {
    Write-Host "No scheduled tasks found (nothing to remove)."
}
