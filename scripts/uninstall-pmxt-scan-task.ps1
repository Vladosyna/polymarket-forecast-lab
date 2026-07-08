# Removes the out-of-band pmxt Router scan scheduled task.
# Run: powershell -ExecutionPolicy Bypass -File scripts\uninstall-pmxt-scan-task.ps1

$ErrorActionPreference = "Stop"
$TaskName = "PolymarketForecastLabPmxtScan"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    & schtasks.exe /Delete /TN $TaskName /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removed scheduled task '$TaskName' (schtasks)."
    } else {
        Write-Host "No scheduled task '$TaskName' found (nothing to remove)."
    }
}
