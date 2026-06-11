---
name: setup-auto-reviewer
description: >-
    Walk a user through installing and configuring this PR auto-reviewer for
    their own team / repository. Use whenever someone asks "how do I set this
    up?", "help me configure the auto-reviewer", "I want my own review agent",
    "onboard me to the auto-reviewer", or anything similar. Conducts an
    interactive interview about their codebase, review style, focus areas,
    and things to ignore, then writes config.json and (optionally) registers
    the Windows scheduled tasks.
user-invocable: true
---

# Set up the PR auto-reviewer for a new user

Use this skill when a colleague wants to stand up their own copy of this
auto-reviewer against their own repository and team.

The end state is:
- `config.json` written next to `auto_review.py` with their settings,
- the reviewer prompt tailored to *their* codebase (not the Copilot Studio
  one this repo was originally built for),
- the two Windows scheduled tasks registered (auto-review every 5 min,
  daily report at 07:00 Mon–Fri),
- a clean dry-run that picks up at least one of their open PRs.

## Step 1 — Sanity check the environment

Run these and surface any missing/failing item to the user before going
further. Do NOT proceed to step 2 until each is OK or the user
explicitly opts to continue anyway.

```pwsh
gh --version
copilot --version
python --version
gh auth status --hostname <their-host>     # e.g. microsoft.ghe.com or github.com
```

Common fixes to suggest:
- `gh auth login --hostname <their-host>` — for the auth check.
- `winget install GitHub.cli` — if `gh` is missing.
- `winget install --id=GitHub.CopilotCLI` — if `copilot` is missing.
- Python 3.10+ from python.org — if Python is missing or too old.

## Step 2 — Interview the user about their review style

**Do this conversationally, one question per turn.** Don't dump a form.
After each answer, reflect it back briefly ("Got it — TypeScript monorepo
for an invoice product, will inject that as codebase context.") so they
can correct on the fly.

Ask, in order:

1. **GitHub host and repo.**
   "What host is the repo on (e.g. `github.com` or `microsoft.ghe.com`)?
    And what's the `owner/name` of the repo to review?"

2. **Daily report email.**
   "Where should the 07:00 Mon–Fri summary email get sent? (Blank to skip
    the daily report entirely.)"

3. **Codebase description.**
   "Pitch your codebase to a senior engineer in one sentence. This gets
    inlined into the reviewer prompt so the model knows what it's reading.
    Concrete examples: 'a TypeScript / React / Node monorepo for the
    Copilot Studio agent designer', 'a Python Django app handling B2B
    invoice ingestion', 'a Go microservice that brokers Kafka to Postgres'."

4. **Focus areas (3–6 items).**
   "What does this reviewer absolutely need to catch in your codebase, on
    top of the built-in defaults (correctness, security, perf, architecture,
    dependency hygiene)? Shorthand is fine — you can say things like
    'efficiency', 'syntax', 'concurrency', 'API contracts', 'telemetry'.
    I'll expand them into concrete reviewer guidance using your codebase
    context, and you can preview before we commit."

5. **Things the reviewer should NEVER comment on.**
   "What kinds of comments would feel like noise on your team's PRs?
    Examples: Storybook story formatting, generated GraphQL types,
    translation file ordering, anything Prettier handles."

6. **Reviewer voice (optional).**
   "Any preferences on tone? Examples: 'Be terse like a senior eng — never
    say \"consider X\" without saying what and why' versus 'Be supportive
    and explanatory, many authors are interns this summer.'"

## Step 3 — Write the config

Either:

a) **Drive `setup.py` interactively** in a terminal (recommended if the
   user is in their own shell — it handles Copilot-powered shorthand
   elaboration end-to-end with preview/accept), or

b) **Synthesize `config.json` yourself** from the interview answers and
   write it to `config.json` next to `auto_review.py`. If they gave you
   shorthand (e.g. "efficiency", "syntax"), elaborate it yourself into
   1-3 concrete sentences each — using the codebase description from
   step 3 as context — before writing. Tell the user what you elaborated
   each shorthand into. Fields:

```json
{
  "gh_host": "...",
  "repo": "owner/name",
  "report_recipient": "user@company.com",
  "codebase_description": "one-sentence pitch",
  "review_focus": ["...", "..."],
  "review_avoid": ["...", "..."],
  "reviewer_style": "free-form prose, optional"
}
```

Confirm with the user what you're about to write, then write it. If they
later want to re-elaborate (e.g. after editing config by hand), they can
run `python setup.py --elaborate`.

## Step 4 — Dry-run validation

```pwsh
cd <repo-root>
python auto_review.py --dry-run --verbose
```

Expected: it prints `Found N open non-draft PR(s) with review requested for
<their-login>`, then for each PR either `reviewed:<verdict>` or `skipped`.
JSON artifacts land in `reviews/`.

If `N == 0`: have them get themselves added as a reviewer on any open PR
and retry. Use `--only-pr <num> --force` against a specific PR they're
authorized on to test the path end-to-end without waiting.

Inspect one artifact (`reviews/pr-<num>-<sha>.json`) with them and
verify the verdict, summary, and inline comments look right. If the
review is off-tone or missing key concerns, edit `config.json` and rerun.

## Step 5 — Schedule it

Only after a clean dry-run:

```pwsh
.\register_scheduled_task.ps1            # dry-run mode (still no GH posts)
.\register_scheduled_task.ps1 -Live      # POSTS reviews to GH
.\register_daily_report_task.ps1         # 07:00 Mon-Fri summary email
```

`setup.py` will offer to do these at the end if the user runs the wizard
interactively.

## Step 6 — Verify the task is alive

```pwsh
Get-ScheduledTask -TaskName AgenticAutomations-AutoReview |
    Select-Object TaskName, State, @{n='Next';e={(Get-ScheduledTaskInfo $_).NextRunTime}}
Get-Content auto_review.log -Tail 30
```

Confirm `State` is `Ready` and the log shows recent activity.

## Notes on what NOT to change

- The state machine, reconsider logic, dismissals, fingerprint short-circuit
  in `auto_review.py` are codebase-agnostic and should not be touched per
  user. Only the prompt-customization fields in `config.json` should
  differ per install.
- `state.json` is per-install runtime state — never copy it between users.
- `reviews/` artifacts are per-install — same.
