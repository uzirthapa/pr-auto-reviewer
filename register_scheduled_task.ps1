# Registers a Windows Scheduled Task to run auto_review.py every 20 minutes.
# Run this script ONCE from an elevated PowerShell (Run as Administrator).
#
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\register_scheduled_task.ps1                       # dry-run mode (safe default)
#   .\register_scheduled_task.ps1 -Live                 # post reviews for real
#   .\register_scheduled_task.ps1 -Unregister           # remove the task
#
# The task logs to auto_review.log next to the script.

param(
    [switch]$Live,
    [switch]$Unregister,
    [string]$TaskName = "AgenticAutomations-AutoReview",
    [int]$IntervalMinutes = 20
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript  = Join-Path $scriptDir "auto_review.py"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered task: $TaskName"
    } else {
        Write-Host "No task named '$TaskName' found."
    }
    return
}

if (-not (Test-Path $pyScript)) { throw "auto_review.py not found at $pyScript" }

$python = (Get-Command python -ErrorAction Stop).Source
$pyArgs = "`"$pyScript`""
if (-not $Live) { $pyArgs += " --dry-run" }

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $pyArgs `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
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
    -Description "Auto-review agentic-automations PRs every $IntervalMinutes min ($(if($Live){'LIVE'}else{'DRY-RUN'}))." | Out-Null

Write-Host "Registered task '$TaskName' (every $IntervalMinutes min, $(if($Live){'LIVE'}else{'DRY-RUN'}))."
Write-Host "Command: $python $pyArgs"
Write-Host "Logs   : $(Join-Path $scriptDir 'auto_review.log')"
