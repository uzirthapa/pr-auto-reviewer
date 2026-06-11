# Bootstrap the auto-reviewer on a new machine.
#
# What it does:
#   1. Verifies git, gh, copilot, python are on PATH (warns otherwise).
#   2. Clones the repo into the target directory (default: current dir).
#   3. cd's in and runs `python setup.py` (interactive wizard).
#
# Usage from a fresh PowerShell:
#
#   One-liner from the raw URL:
#     iwr https://microsoft.ghe.com/raw/uzirthapa/agentic-automations-auto-review/main/install.ps1 -UseDefaultCredentials | iex
#
#   Or download + run with options:
#     ./install.ps1                       # clone into .\agentic-automations-auto-review
#     ./install.ps1 -Dir C:\tools\reviewer
#     ./install.ps1 -RepoUrl <fork url>   # if you've forked
#     ./install.ps1 -NoSetup              # clone only, skip wizard

param(
    [string]$RepoUrl = "https://microsoft.ghe.com/uzirthapa/agentic-automations-auto-review.git",
    [string]$Dir = (Join-Path (Get-Location) "agentic-automations-auto-review"),
    [switch]$NoSetup
)

$ErrorActionPreference = "Stop"

function _ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function _warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function _fail($msg) { Write-Host "  x $msg" -ForegroundColor Red }

Write-Host "`nAuto-Reviewer install"
Write-Host "  Source: $RepoUrl"
Write-Host "  Target: $Dir"

Write-Host "`n[1/3] Prereq check"
$missing = @()
foreach ($cmd in @("git","gh","copilot","python")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) { _ok "$cmd -> $($found.Source)" }
    else        { _fail "$cmd NOT FOUND"; $missing += $cmd }
}
if ($missing.Count -gt 0) {
    _warn "Missing: $($missing -join ', ')"
    _warn "Fix suggestions:"
    if ($missing -contains "git")     { _warn "  winget install Git.Git" }
    if ($missing -contains "gh")      { _warn "  winget install GitHub.cli" }
    if ($missing -contains "copilot") { _warn "  winget install GitHub.CopilotCLI" }
    if ($missing -contains "python")  { _warn "  winget install Python.Python.3.12" }
    Write-Host ""
    $continue = Read-Host "Continue anyway? [y/N]"
    if ($continue -notmatch '^(y|yes)$') { exit 1 }
}

Write-Host "`n[2/3] Clone"
if (Test-Path $Dir) {
    _warn "$Dir already exists. Pulling latest instead of cloning."
    Push-Location $Dir
    try { git pull --ff-only } finally { Pop-Location }
} else {
    git clone $RepoUrl $Dir
    _ok "Cloned into $Dir"
}

if ($NoSetup) {
    Write-Host "`n-NoSetup specified. Skipping wizard. Run later:"
    Write-Host "    cd $Dir; python setup.py"
    exit 0
}

Write-Host "`n[3/3] Setup wizard"
Push-Location $Dir
try {
    $env:PYTHONIOENCODING = "utf-8"
    python setup.py
}
finally {
    Pop-Location
}

Write-Host "`nDone. Next:"
Write-Host "  cd $Dir"
Write-Host "  python auto_review.py --dry-run --verbose    # validate"
Write-Host "  .\register_scheduled_task.ps1 -Live          # go live"
