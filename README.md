# Agentic Automations Auto-Review

Python tooling that auto-reviews open PRs on a GitHub (or GitHub Enterprise)
repo **where the authenticated user is a requested reviewer** (i.e., the
same PRs you'd see in your "Awaiting your review" list). Python handles
all GitHub I/O via `gh`; the only thing handed to `copilot` is the
reasoning task, with a strict JSON contract.

> Originally built for `microsoft.ghe.com/bic/agentic-automations`; now
> configurable so any team can stand up their own instance against any
> repo. See **Sharing this with your team** below.

## What it does each cycle
For every open, non-draft PR that has me on the reviewers list, the script
picks one of:

| state                                                          | action                                  |
|----------------------------------------------------------------|-----------------------------------------|
| never reviewed                                                 | full **review**                          |
| prior state exists, no new activity, HEAD unchanged            | **skip**                                 |
| HEAD SHA changed since prior action                            | **reconsider** with new diff             |
| author replied / commented since prior action                  | **reconsider**                            |
| someone re-requested me as a reviewer since prior action       | **reconsider**                            |

`state.json` records `head_sha`, decision, our review id, submission
time, and per-reconsideration history. Reconsider applies regardless of
whether the prior decision was `request_changes`, `comment`, or
`approve` — any meaningful change (new commits, author reply, explicit
re-request) re-engages the model.

The "reconsider" prompt gives Copilot the original review, every author
reply (issue comments + inline review comments), new commits, and any
explicit re-review requests targeting us — plus the current diff — and
asks for a fresh decision (`approve` / `request_changes` / `comment`)
with `addresses_prior_block` (`true` / `false` / `null` if prior wasn't
a block) and `remaining_concerns`.

Copilot writes its answer to `review_output.json` in a sandbox directory
(passed via `--add-dir`); the script reads and validates that file —
much more robust than parsing stdout when the CLI prints progress chrome.

## Files
- `auto_review.py` — main script
- `register_scheduled_task.ps1` — registers a Windows Scheduled Task that
  runs every 20 minutes
- `state.json` — per-PR state (auto-managed)
- `reviews/` — JSON artifacts of every review / reconsideration (full text)
- `reviews/metrics.jsonl` — append-only lean ledger (one row per review)
  for impact reporting. Counts + pointers only; full issue text stays in
  the per-PR artifact (`artifact_path` field links them).
- `auto_review.log` — rolling log

## Prereqs
- `gh` authenticated for `microsoft.ghe.com`
  (`gh auth status --hostname microsoft.ghe.com`)
- `copilot` CLI on PATH
- Python 3.10+

## Usage

Dry-run (does not post anything; prints + writes artifacts):
```
python auto_review.py --dry-run
```

Single PR, verbose:
```
python auto_review.py --dry-run --only-pr 11372 --verbose
```

Live (posts approve / request-changes / comment reviews via `gh pr review`):
```
python auto_review.py
```

Force re-review even if HEAD SHA is unchanged:
```
python auto_review.py --dry-run --force
```

## Scheduling (every 20 min)
From an elevated PowerShell:
```
cd C:\Users\uzirthapa\CodeReviewAgentDesigner
.\register_scheduled_task.ps1            # dry-run schedule
.\register_scheduled_task.ps1 -Live      # live schedule
.\register_scheduled_task.ps1 -Unregister
```

## Tuning
Environment variables:
- `COPILOT_REVIEW_MODEL` (default `claude-opus-4.7-1m-internal`)
- `COPILOT_REVIEW_EFFORT` (default `high`)
- `COPILOT_REVIEW_TIMEOUT` seconds (default `900`)

## Daily morning report
A second Scheduled Task emails a summary of the prior 24 hours of reviews
(decisions, distinct PRs, issues raised, blocks issued/lifted, per-PR
activity) to your inbox at 07:00 local time via Outlook COM (uses your
already-signed-in Outlook profile — no auth setup needed).

Preview only (writes HTML to a file, doesn't send):
```
python send_daily_report.py --dry-run --verbose
```

Send now (live):
```
python send_daily_report.py --verbose
```

Register / unregister the 07:00 weekday task (Mon-Fri only — weekends are skipped):
```
.\register_daily_report_task.ps1                 # default 07:00 Mon-Fri
.\register_daily_report_task.ps1 -At "08:30"     # custom time
.\register_daily_report_task.ps1 -Unregister
```

The script also has a safety net: a live send (`python send_daily_report.py`)
on a Saturday or Sunday no-ops with a log line. Pass `--include-weekends` to
override. `--dry-run` always renders regardless of day.

Environment variables:
- `REPORT_RECIPIENT` (default `uzirthapa@microsoft.com`)
- `REPORT_HOURS` window in hours (default `24`)

Logs go to `daily_report.log`. Source data is `reviews/metrics.jsonl`
appended by `auto_review.py` every cycle.

## Sharing this with your team

Anything codebase-specific (host, repo, reviewer prompt focus,
things-to-ignore, recipient email) lives in `config.json` next to the
scripts. The code itself is generic.

To onboard a teammate:

1. They clone the repo:
   ```pwsh
   git clone <this repo url>
   cd CodeReviewAgentDesigner
   ```
2. They run the interactive wizard:
   ```pwsh
   python setup.py
   ```
   It checks prereqs (`gh`, `copilot`, `python`, `gh auth`), then asks
   them about:
   - GitHub host + repo to review
   - Daily-summary recipient email
   - One-sentence codebase description (injected into the reviewer prompt)
   - Focus areas the reviewer should ALWAYS look for
   - Things the reviewer should NEVER comment on
   - Reviewer voice / style preferences

   **Shorthand is fine.** If they type bullets like `efficiency`,
   `syntax`, or `concurrency`, the wizard will offer to expand them via
   Copilot using their codebase context into detailed reviewer guidance
   (e.g. "Flag O(n^2) loops over arrays that can be large, missed
   memoization in hot React renders, N+1 fetches where a batched call
   would do..."). They preview the elaboration and accept/reject before
   it's written to `config.json`.

   To re-elaborate later after hand-editing `config.json`:
   ```pwsh
   python setup.py --elaborate
   ```

   It writes `config.json` and optionally registers the two Windows
   scheduled tasks for them.
3. They dry-run:
   ```pwsh
   python auto_review.py --dry-run --verbose
   ```
4. Once they like the artifacts in `reviews/`, they register the live task:
   ```pwsh
   .\register_scheduled_task.ps1 -Live
   .\register_daily_report_task.ps1
   ```

### Walk-through skill

A Copilot CLI skill ships with the repo at
`.copilot/skills/setup-auto-reviewer/SKILL.md`. After cloning, copy or
symlink it into your user skills folder (`%USERPROFILE%\.copilot\skills\`)
and ask Copilot CLI:
> "Help me set up the auto-reviewer"

The skill conducts the interview conversationally (one question per turn,
reflecting answers back) and synthesizes `config.json` directly, then
walks through dry-run validation and scheduling.

### Files that ship; files that are per-install

| Ships in git                                            | Per-install (gitignored)                |
| ------------------------------------------------------- | --------------------------------------- |
| `auto_review.py`, `send_daily_report.py`, `daily_report.py`, `setup.py`, `config.py`, `rerun_comment_verdicts.py` | `config.json`                           |
| `register_scheduled_task.ps1`, `register_daily_report_task.ps1` | `state.json`                            |
| `config.example.json`                                   | `auto_review.log`, `daily_report.log`   |
| `.copilot/skills/setup-auto-reviewer/SKILL.md`          | `reviews/` (artifacts + `metrics.jsonl`) |
| `README.md`, `.gitignore`                               |                                          |
