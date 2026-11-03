param(
    [string]$Time = "21:00",
    [string]$TaskName = "Notion Calendar Analytics Export"
)

$ErrorActionPreference = "Stop"
$programPath = Join-Path $PSScriptRoot "notion_calendar_export.exe"
$outputPath = Join-Path $PSScriptRoot "exports\latest"
$arguments = "--output-dir `"$outputPath`""
$action = New-ScheduledTaskAction -Execute $programPath -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Export all databases under the Notion Calendar Analytics Parent databases page." -Force | Out-Null
Write-Host "已创建每日 $Time 运行的任务：$TaskName"
Write-Host "请确认当前 Windows 用户环境变量 NOTION_API_KEY 已设置，并已在 Notion 中分享模板页与所有源数据库给集成。"
