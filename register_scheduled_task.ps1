# Registers a Windows Scheduled Task to run auto_review.py every 5 minutes.
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
    [int]$IntervalMinutes = 5
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
# Use pythonw.exe so the scheduled task runs without flashing a console
# window every cycle. Falls back to python.exe if pythonw isn't present.
$pythonw = $python -replace 'python\.exe$', 'pythonw.exe'
if (Test-Path $pythonw) { $python = $pythonw }
$pyArgs = "`"$pyScript`""
if (-not $Live) { $pyArgs += " --dry-run" }

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument $pyArgs `
    -WorkingDirectory $scriptDir

# Two triggers so the task is resilient across reboots, sleep, and the
# normal 5-min cadence:
#   1. Time trigger that repeats every $IntervalMinutes forever.
#   2. At-logon trigger so the very first run happens immediately after
#      the user signs in following a reboot (instead of waiting up to
#      $IntervalMinutes for the next time-trigger tick).
$timeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$triggers = @($timeTrigger, $logonTrigger)

# WakeToRun lets the machine come out of sleep to hit a scheduled tick.
# RestartCount/RestartInterval auto-retry a failed run a few times rather
# than waiting the full $IntervalMinutes for the next cycle.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
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
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Auto-review agentic-automations PRs every $IntervalMinutes min ($(if($Live){'LIVE'}else{'DRY-RUN'}))." | Out-Null

Write-Host "Registered task '$TaskName' (every $IntervalMinutes min, $(if($Live){'LIVE'}else{'DRY-RUN'}))."
Write-Host "Command: $python $pyArgs"
Write-Host "Logs   : $(Join-Path $scriptDir 'auto_review.log')"
