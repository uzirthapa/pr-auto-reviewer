# AGENTS.md

Guidance for AI coding agents working in this repo. Read this first.

## What this repo is

A Python + Windows-Task-Scheduler harness that auto-reviews open PRs on a
GitHub (or GHE) repo, scoped to PRs opened by a configured set of authors
(`review_authors`).
Originally targeted `microsoft.ghe.com/bic/agentic-automations`; now
configurable per-install via `config.json`.

**Hard rule:** Python handles all GitHub I/O. The Copilot CLI is invoked
*only* for the reasoning step (initial review or reconsideration) with a
strict JSON contract. Do not move GitHub API calls into the model
prompt, and do not give the model tools to call `gh` itself.

## Architecture at a glance

```
list_open_prs  ──►  fingerprint short-circuit  ──►  per-PR loop
                                                      │
                                                      ├─ review_pr (new PR)
                                                      │     └─► run_copilot_review
                                                      │           └─► submit_review_via_api
                                                      │
                                                      └─ reconsider_pr (activity since last)
                                                            └─► run_copilot_reconsider
                                                                  └─► submit_review_via_api
                                                                  └─► dismiss_review (if block→comment)
```

Per-cycle flow (every 5 min via Scheduled Task):
1. For each login in `review_authors`, `gh pr list ... author:<login>`,
   unioned → `PullRequest` list. An author search always surfaces the PR
   regardless of our review state, so it also catches authors pushing new
   commits AFTER we reviewed (no separate `reviewed-by:@me` sweep needed).
   With no `review_authors` configured, nothing is in scope.
2. SHA-256 fingerprint of `[(number, head_sha, updated_at), ...]`. If
   unchanged since last run and not `--force` / `--only-pr`, exit early
   without per-PR work.
3. For each PR, `decide_action(state, pr)` returns one of:
   - `skip` — already handled at this HEAD, no new author activity.
   - `review` — never reviewed, or HEAD moved with prior non-block decision
   - `reconsider` — prior decision was a block, or author replied / pushed
     / re-requested us since our last action

Initial reviews are **binary** (`approve` | `request_changes`).
Reconsiders are **3-state** (`approve` | `comment` | `request_changes`) —
`comment` is used when an author has rebutted our block with a rationale
we don't fully agree with but no longer want to block on; we then
explicitly dismiss our prior `CHANGES_REQUESTED` via the
`/pulls/{n}/reviews/{id}/dismissals` API so branch protection clears.

## Files

| File                                          | Role                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------- |
| `auto_review.py`                              | Main script. All review / reconsider logic, GH I/O, state mgmt.      |
| `daily_report.py`                             | Reads `reviews/metrics.jsonl`, renders HTML summary of last 24h.     |
| `send_daily_report.py`                        | Calls `daily_report.render_html`, sends via Outlook COM (PowerShell). |
| `rerun_comment_verdicts.py`                   | One-off backfill: re-run reconsider on PRs whose stored verdict was the legacy `comment`. |
| `config.py`                                   | Tiny loader for `config.json`. No fallbacks logic here — defaults live in the consumers. |
| `setup.py`                                    | Interactive wizard for new users. Writes `config.json`. Offers to register tasks. Calls `copilot` to elaborate shorthand focus/avoid/style items into detailed reviewer guidance (`--elaborate` re-runs just that). |
| `register_scheduled_task.ps1`                 | Registers the every-5-min `AgenticAutomations-AutoReview` task.      |
| `register_daily_report_task.ps1`              | Registers the Mon-Fri 07:00 `AgenticAutomations-DailyReport` task.   |
| `install.ps1`                                 | One-line bootstrap installer for teammates: prereq-checks, clones, runs `setup.py`. Invoked via `iwr ... | iex`. |
| `.copilot/skills/setup-auto-reviewer/SKILL.md`| Copilot CLI skill walking new users through `setup.py`.              |
| `config.example.json`                         | Documented template colleagues copy / edit.                          |
| `config.json` *(gitignored)*                  | Per-install overrides. Missing is fine — defaults reproduce my live setup. |
| `state.json` *(gitignored)*                   | Per-PR runtime state (head_sha, decision, review_id, reconsider history, top-level `__last_prs_fingerprint`). |
| `reviews/pr-<num>-<sha>.json` *(gitignored)*  | Full per-review artifact (prompt + raw JSON response + diff metadata). |
| `reviews/metrics.jsonl` *(gitignored)*        | Append-only one-row-per-review ledger feeding the daily report.      |
| `auto_review.log`, `daily_report.log` *(gitignored)* | Rolling logs.                                                 |

## Conventions

- **One config field maps to one user-visible setting.** If you add a new
  knob, plumb it through: `config.example.json` (with example value),
  consumer reads via `config.get("key", <safe_default>)`, and `setup.py`
  has an interview question for it. Don't add settings as env vars only
  — env vars are for ops overrides, `config.json` is the source of truth.

- **Defaults in code MUST reproduce the original Copilot-Studio behavior
  byte-for-byte** when `config.json` is missing. The author's live install
  has no config file. If you can't preserve that, ship a `config.json`
  alongside your change for him.

- **Prompt edits go in `REVIEW_INSTRUCTIONS_TEMPLATE` /
  `RECONSIDER_INSTRUCTIONS`** in `auto_review.py`. Placeholders use the
  `__UPPER_SNAKE__` token form (not `str.format`, not `Template`) because
  the prompt contains literal `{...}` JSON-schema braces. Do not switch
  to `.format()` or f-strings here.

- **State machine in `reconsider_pr` matters; preserve the early-exit
  order:**
  1. activity gate (skip if no activity & HEAD unchanged)
  2. already-approved + no code change → skip
  3. (fetch diff + run model)
  4. unchanged verdict + no code change → record-only, no GH post
  5. post review; if `request_changes → comment`, also dismiss prior block
  6. `_record_reconsider` advances watermark

  Reordering these will reintroduce spam.

- **Validators:** `_validate_review` is binary-only by default;
  `_validate_reconsider` (and any future allow-3-state caller) MUST pass
  `allow_comment=True`. Initial reviews coerce stray `comment → approve`.

- **Dead stub pattern:** When superseding a function, rename the old one
  to `<name>_LEGACY_REMOVED` and make it `raise NotImplementedError`.
  This is a recurring bite point — silent duplicate definitions have
  shadowed real implementations twice in this repo's history.

- **Never write to `state.json` outside `save_state(state)`.** Persist
  after every PR so a crash doesn't lose progress. Don't batch.

- **Logging:** `logging.info` for normal flow, `logging.warning` for
  recoverable oddness, `logging.error` for failures we couldn't act on,
  `logging.exception` only inside `except` blocks where we want a
  traceback in `auto_review.log`. Don't `print` from hot paths.

- **Console encoding:** Windows cmd is often cp1252. Any new script that
  prints non-ASCII must do
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` early in
  `main()`. `setup.py` already does this; prefer ASCII art over Unicode
  box-drawing characters in interactive output.

- **No new dependencies without strong justification.** Stdlib-only.
  External CLIs: `gh`, `copilot`, `powershell.exe`. Everything else
  through `subprocess.run(..., timeout=...)` — always set a timeout.

## When making changes

Run these before considering work done. None are CI-enforced, but they're
the project's smoke tests:

```pwsh
# 1. Imports cleanly with no config (preserves live install behavior).
python -c "import auto_review, send_daily_report, daily_report, config, setup; print('ok')"

# 2. Live dry-run on real PRs. Should print Found N PRs and not raise.
python auto_review.py --dry-run

# 3. Daily report render still works.
python send_daily_report.py --dry-run --verbose

# 4. Setup wizard end-to-end (non-interactive, writes to temp).
$env:PYTHONIOENCODING="utf-8"
python -c "import tempfile, json, setup, pathlib; setup.CONFIG_PATH = pathlib.Path(tempfile.mktemp(suffix='.json')); cfg = setup.collect_config({'gh_host':'github.com','repo':'o/r','codebase_description':'x'}, non_interactive=True); setup.write_config(cfg); print(setup.CONFIG_PATH.read_text())"
```

## Things NOT to do

- **Don't hand the model GitHub tools or browsing access.** The whole
  design depends on a deterministic, auditable JSON contract.
- **Don't expand the initial-review decision space to include `comment`.**
  Initial reviews are binary by design — `comment` only exists as a
  reconsider verdict for the "drop block, defer to author" transition.
- **Don't introduce per-author state outside `state.json`.** Don't bolt
  on a database. Don't move state into the model.
- **Don't add a new scheduled task without an `Unregister` switch and
  without setting `MultipleInstances IgnoreNew`.**
- **Don't rewrite git history** (per user preference — applies project-wide).
- **Don't commit `state.json`, `config.json`, anything under `reviews/`,
  or any `*.log`.** All gitignored — keep it that way.
- **Don't introduce nondeterministic prompts** (no random sampling, no
  time-of-day–varying instructions). Same PR + same HEAD should produce
  the same verdict on rerun.

## Pointers to the load-bearing logic

| Concern                        | Where                                                      |
| ------------------------------ | ---------------------------------------------------------- |
| PR listing (author:<login> per review_author) + fingerprint | `list_open_prs`, `compute_prs_fingerprint` |
| Decision routing               | `review_pr` (initial path) / `reconsider_pr` (any prior-state path) |
| Initial prompt + rendering     | `REVIEW_INSTRUCTIONS_TEMPLATE`, `_render_review_instructions` |
| Reconsider state machine prompt| `RECONSIDER_INSTRUCTIONS`                                   |
| JSON validators                | `_validate_review` (binary by default), `_validate_reconsider` |
| Review submission              | `submit_review_via_api`                                     |
| Block dismissal                | `find_my_latest_blocking_review_id`, `dismiss_review`       |
| State persistence              | `load_state`, `save_state`, `_record_initial_review`, `_record_reconsider` |
| Activity detection             | `fetch_pr_activity_since`, `activity_warrants_reconsider`   |
| Per-install config             | `config.py` + `_user_config.get(...)` callsites             |

If you can't find what you need from this table, grep before refactoring.
