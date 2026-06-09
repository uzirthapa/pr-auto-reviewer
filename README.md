# Agentic Automations Auto-Review

Python tooling that auto-reviews open PRs on
`microsoft.ghe.com/bic/agentic-automations` **where the authenticated user is
a requested reviewer** (i.e., the same PRs you'd see in your GitHub
"Awaiting your review" list). Python handles all GitHub I/O via `gh`; the
only thing handed to `copilot` is the reasoning task, with a strict JSON
contract.

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
