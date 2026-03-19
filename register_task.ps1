param(
    [string]$TaskName = "CampusWiFiAutoLogin",
    [string]$RunAt = "07:30"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSCommandPath
$batPath = Join-Path $root "login.bat"

if (-not (Test-Path $batPath)) {
    throw "找不到启动脚本：$batPath"
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
    -Description "校园网自动登录任务，07:30 执行，支持唤醒和桌面通知。" `
    -Force | Out-Null

Write-Host "计划任务已更新："
Write-Host "  名称: $TaskName"
Write-Host "  时间: $RunAt"
Write-Host "  用户: $userId"
Write-Host "  唤醒运行: true"
Write-Host "  仅网络可用时运行: false"
Write-Host "  登录类型: Interactive"
