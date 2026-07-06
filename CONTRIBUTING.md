# Contributing

Thanks for your interest in improving the PR Auto-Reviewer! This document
covers how to get set up, the conventions the project follows, and how to
get a change merged.

## Getting started

1. **Fork and clone** the repository.
2. **Prereqs:** Python 3.10+, the `gh` CLI (authenticated for your host),
   and at least one supported AI CLI on your PATH (`copilot`, `agency`, or
   `claude`). See the [README](README.md) for details.
3. **Configure a local install:** run `python setup.py` to generate a
   `config.json` (gitignored). A missing `config.json` is fine for imports,
   but you'll need one to run against a real repo.

There are no third-party Python dependencies — the project is **stdlib-only**
by design, so there is nothing to `pip install`.

## Project layout

The [README](README.md) has a full file-by-file table. The load-bearing
logic lives in `auto_review.py` (PR listing, the review/reconsider state
machine, all GitHub I/O, and the pluggable AI-CLI invocation).

## Conventions

- **Stdlib-only.** Do not add third-party Python dependencies. External
  CLIs are limited to `gh`, the configured AI CLI, and `powershell.exe`.
  Always pass a `timeout=` to `subprocess.run(...)`.
- **Python does all GitHub I/O.** The AI CLI is invoked *only* for the
  reasoning step, with a strict JSON contract. Do not give the model tools
  to call `gh`, and do not move GitHub API calls into the prompt.
- **One config field = one user-visible setting.** New knobs must be
  plumbed through: `config.example.json` (with an example value), read via
  `config.get("key", <safe_default>)`, and given an interview question in
  `setup.py`.
- **Defaults must be generic.** Code defaults should work for a fresh
  clone (e.g. `github.com`, empty repo). Anything install-specific belongs
  in the gitignored `config.json`.
- **Determinism.** The same PR at the same HEAD must produce the same
  verdict on rerun — no random sampling, no time-of-day-varying prompts.
- **Logging, not printing,** from hot paths: `logging.info` for normal
  flow, `logging.warning` for recoverable oddness, `logging.error` for
  failures, `logging.exception` inside `except` blocks.
- **Never commit** `config.json`, `state.json`, anything under `reviews/`
  or `repos/`, or any `*.log` — they are gitignored, keep it that way.

Please keep changes surgical and focused. Update documentation
(`README.md`, `config.example.json`) alongside any user-facing change.

## Testing your changes

There is no CI-enforced test suite, but these are the project's smoke
tests. Run the relevant ones before opening a PR:

```pwsh
# 1. Imports cleanly with no config (preserves default behavior).
python -c "import auto_review, send_daily_report, daily_report, config, setup; print('ok')"

# 2. Live dry-run on real PRs. Should print "Found N PRs" and not raise.
python auto_review.py --dry-run

# 3. Daily report render still works.
python send_daily_report.py --dry-run --verbose

# 4. Setup wizard end-to-end (non-interactive, writes to a temp file).
$env:PYTHONIOENCODING="utf-8"
python -c "import tempfile, json, setup, pathlib; setup.CONFIG_PATH = pathlib.Path(tempfile.mktemp(suffix='.json')); cfg = setup.collect_config({'gh_host':'github.com','repo':'o/r','codebase_description':'x'}, non_interactive=True); setup.write_config(cfg); print(setup.CONFIG_PATH.read_text())"
```

`--dry-run` never posts to GitHub — it prints and writes artifacts only, so
it's always safe to run.

## Submitting a pull request

1. Create a branch off `main`.
2. Make your change, run the smoke tests above.
3. Open a PR with a clear description of *what* changed and *why*.
4. Keep the diff minimal; avoid unrelated refactors.

## Reporting bugs and requesting features

Open a GitHub issue with clear reproduction steps (for bugs) or a concrete
use case (for features). Include your OS, Python version, and which AI
provider (`ai_provider`) you're using.

## Platform note

The reviewer core (`auto_review.py`) is cross-platform, but the scheduling
scripts (`register_*.ps1`, Windows Task Scheduler) and the daily email
(Outlook COM) are **Windows-only**. Contributions that add cron/systemd
equivalents or an SMTP email path are welcome.

## License

By contributing, you agree that your contributions will be licensed under
the [MIT License](LICENSE).
