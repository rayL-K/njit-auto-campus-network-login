param(
    [string]$TaskName = "CampusWiFiAutoLogin",
    [string]$RunAt = "07:30"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSCommandPath
$batPath = Join-Path $root "login.bat"

if (-not (Test-Path $batPath)) {
    throw "Missing launcher: $batPath"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 6 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$settings.RunOnlyIfNetworkAvailable = $false
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Campus WiFi auto-login at 07:30 with wake support and desktop notifications." `
    -Force | Out-Null

Write-Host "Scheduled task updated:"
Write-Host "  Name: $TaskName"
Write-Host "  Time: $RunAt"
Write-Host "  User: $userId"
Write-Host "  WakeToRun: true"
Write-Host "  RunOnlyIfNetworkAvailable: false"
Write-Host "  LogonType: Interactive"
