# Installs Agent Router as a Windows Scheduled Task.
# Run PowerShell as Administrator:
# powershell -ExecutionPolicy Bypass -File .\install-agent-router-task.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $Root "start-agent-router.ps1"
$TaskName = "Agent Router - Continuous"
if (-not (Test-Path $Runner)) { throw "Runner not found: $Runner" }
$PowerShell = (Get-Command powershell.exe).Source
$Args = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Args -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Keeps the Agent Router gateway running continuously for Antigravity." | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed: $TaskName"
Write-Host "Router: http://127.0.0.1:8001/api"
Write-Host "Health: http://127.0.0.1:8001/health"
