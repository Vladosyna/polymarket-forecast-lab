# Registers a Windows Scheduled Task that runs the watchdog every hour (and at
# logon). The watchdog starts the Forecast Lab orchestrator if it is not already
# running -- so the lab effectively stays up on its own.
#
# Run once:   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
# Remove:     powershell -ExecutionPolicy Bypass -File scripts\uninstall-watchdog.ps1

$ErrorActionPreference = "Stop"

$Root     = Split-Path -Parent $PSScriptRoot
$Watchdog = Join-Path $Root "scripts\watchdog.ps1"
$TaskBat  = Join-Path $Root "scripts\watchdog-task.bat"
$TaskName = "PolymarketForecastLabWatchdog"
$HourlyTaskName = "PolymarketForecastLabWatchdogHourly"

if (-not (Test-Path $Watchdog)) {
    throw "watchdog.ps1 not found at $Watchdog"
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$action = New-ScheduledTaskAction `
    -Execute $TaskBat `
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

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $atLogon `
        -Settings $settings `
        -Principal $principal `
        -Description "At logon: start the Forecast Lab orchestrator and dashboard if they are not running." `
        -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName' (at logon)."
    $schedulerOk = $true
} catch {
    Write-Warning "PowerShell Register-ScheduledTask failed: $_"
    Write-Host "Trying schtasks fallback..."
    & schtasks.exe /Create /TN $TaskName /TR "`"$TaskBat`"" /SC ONLOGON /RL LIMITED /F
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Registered scheduled task '$TaskName' via schtasks (at logon)."
        $schedulerOk = $true
    } else {
        Write-Warning "schtasks logon registration failed (exit $LASTEXITCODE)."
        $schedulerOk = $false
    }
}

if ($schedulerOk) {
    try {
        Register-ScheduledTask `
            -TaskName $HourlyTaskName `
            -Action $action `
            -Trigger $hourly `
            -Settings $settings `
            -Principal $principal `
            -Description "Hourly: start the Forecast Lab orchestrator and dashboard if they are not running." `
            -Force | Out-Null
        Write-Host "Registered scheduled task '$HourlyTaskName' (hourly)."
    } catch {
        Write-Warning "PowerShell hourly task failed: $_"
        & schtasks.exe /Create /TN $HourlyTaskName /TR "`"$TaskBat`"" /SC HOURLY /MO 1 /F
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Registered scheduled task '$HourlyTaskName' via schtasks (hourly)."
        } else {
            Write-Warning "schtasks hourly registration failed (exit $LASTEXITCODE)."
        }
    }
} else {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $runCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`""
    Set-ItemProperty -Path $runKey -Name "PolymarketForecastLab" -Value $runCmd
    Write-Host "Task Scheduler unavailable -- registered HKCU Run autostart instead."
    Write-Host "For full Task Scheduler (logon + hourly), run scripts\install-autostart.bat as Administrator."
}
Write-Host "Running the watchdog once now to start the lab if it is down..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Watchdog
Write-Host "Done. Check data\logs\watchdog.log for actions."
