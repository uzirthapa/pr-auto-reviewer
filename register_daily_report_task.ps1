# Registers a Windows Scheduled Task to email the auto-review daily report
# at a chosen time of day (default 07:00, Mon-Fri). Runs in the current
# user's session so it can drive Outlook via COM (see send_daily_report.py).
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
# Use pythonw.exe so the daily task doesn't briefly flash a console window.
$pythonw = $python -replace 'python\.exe$', 'pythonw.exe'
if (Test-Path $pythonw) { $python = $pythonw }

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$pyScript`"" `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $At

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

Write-Host "Registered task '$TaskName' (Mon-Fri at $At)."
Write-Host "Command: $python `"$pyScript`""
Write-Host "Logs   : $(Join-Path $scriptDir 'daily_report.log')"
