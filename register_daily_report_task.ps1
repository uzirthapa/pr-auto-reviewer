# Registers a Windows Scheduled Task to email the auto-review daily report
# at 07:00 every day. Runs in the current user's session so it can use the
# already-authenticated `copilot` CLI and its mail MCP.
#
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\register_daily_report_task.ps1                  # 07:00 daily, real send
#   .\register_daily_report_task.ps1 -At "08:30"      # different time of day
#   .\register_daily_report_task.ps1 -Unregister      # remove the task
#
# Logs land in daily_report.log next to the script.

param(
    [switch]$Unregister,
    [string]$TaskName = "AgenticAutomations-DailyReport",
    [string]$At = "07:00"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript  = Join-Path $scriptDir "send_daily_report.py"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered task: $TaskName"
    } else {
        Write-Host "No task named '$TaskName' found."
    }
    return
}

if (-not (Test-Path $pyScript)) { throw "send_daily_report.py not found at $pyScript" }
$python = (Get-Command python -ErrorAction Stop).Source

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$pyScript`"" `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Emails the agentic-automations auto-review daily report at $At." | Out-Null

Write-Host "Registered task '$TaskName' (daily at $At)."
Write-Host "Command: $python `"$pyScript`""
Write-Host "Logs   : $(Join-Path $scriptDir 'daily_report.log')"
