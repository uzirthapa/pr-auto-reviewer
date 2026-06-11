#!/usr/bin/env python3
"""Interactive setup wizard for the auto-reviewer.

Walks a new user through:
  1. Verifying prereqs (gh, copilot, python, gh auth).
  2. Collecting per-install settings (host, repo, recipient).
  3. Customizing the reviewer prompt (codebase context, focus areas,
     things to avoid commenting on, reviewer style).
  4. Writing config.json next to the scripts.
  5. Optionally registering the Windows scheduled tasks.

Run interactively:
    python setup.py

Headless / re-runs (skip every prompt that already has a value):
    python setup.py --non-interactive

Re-run any time to update — your existing config is loaded as the
default for each prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
EXAMPLE_PATH = SCRIPT_DIR / "config.example.json"

# Reuse the same Copilot model / effort the reviewer uses, so elaboration
# matches its voice. Elaboration is much shorter than a review so we give
# it a tighter timeout.
COPILOT_MODEL = os.environ.get("COPILOT_REVIEW_MODEL", "claude-opus-4.7-1m-internal")
COPILOT_EFFORT = os.environ.get("COPILOT_SETUP_EFFORT", "medium")
COPILOT_TIMEOUT = int(os.environ.get("COPILOT_SETUP_TIMEOUT", "180"))


# ---------------------------------------------------------------------------
# Tiny prompt helpers
# ---------------------------------------------------------------------------

def _print_header(title: str) -> None:
    bar = "-" * max(8, len(title) + 4)
    print(f"\n{bar}\n  {title}\n{bar}")


def _ask(prompt: str, default: str | None = None, *, required: bool = False,
         non_interactive: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if non_interactive:
        if default is not None:
            return default
        if required:
            raise RuntimeError(f"--non-interactive given but no default for: {prompt}")
        return ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            raw = ""
        if not raw and default is not None:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print("  (value required)")


def _ask_yes_no(prompt: str, default: bool = False, *,
                non_interactive: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    if non_interactive:
        return default
    try:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        raw = ""
    if not raw:
        return default
    return raw in ("y", "yes")


def _ask_list(prompt: str, defaults: list[str] | None = None, *,
              non_interactive: bool = False) -> list[str]:
    """One item per line; empty line ends the list. Existing defaults
    are shown; an empty answer keeps them."""
    if defaults is None:
        defaults = []
    if non_interactive:
        return defaults
    print(f"\n{prompt}")
    if defaults:
        print("  Current items (press Enter to keep, type 'clear' to start over,")
        print("  or type new items to APPEND; finish with a blank line):")
        for d in defaults:
            print(f"    - {d}")
    else:
        print("  One item per line. Finish with a blank line.")
    items = list(defaults)
    first = True
    while True:
        try:
            raw = input("  > ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return items
        if first and raw.lower() == "clear":
            items = []
            first = False
            continue
        first = False
        items.append(raw)


# ---------------------------------------------------------------------------
# Prereq checks
# ---------------------------------------------------------------------------

def _check_command(name: str) -> str | None:
    return shutil.which(name)


def _check_gh_auth(host: str) -> tuple[bool, str]:
    """Return (ok, message)."""
    try:
        res = subprocess.run(
            ["gh", "auth", "status", "--hostname", host],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return False, "`gh` not on PATH"
    except subprocess.TimeoutExpired:
        return False, "`gh auth status` timed out"
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0 and "Logged in" in out:
        return True, out.strip().splitlines()[0] if out.strip() else "logged in"
    return False, out.strip() or "not logged in"


def _gh_viewer_login(host: str) -> str:
    try:
        res = subprocess.run(
            ["gh", "api", "--hostname", host, "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def run_prereq_checks(host: str) -> bool:
    _print_header("Prereq checks")
    ok = True
    for name, label in [("python", "Python"), ("gh", "GitHub CLI"),
                        ("copilot", "Copilot CLI")]:
        p = _check_command(name)
        if p:
            print(f"  ✓ {label:14s} found at {p}")
        else:
            print(f"  ✗ {label:14s} NOT FOUND on PATH")
            ok = False

    auth_ok, msg = _check_gh_auth(host)
    if auth_ok:
        print(f"  ✓ gh auth status ({host}): {msg}")
    else:
        print(f"  ✗ gh auth status ({host}): {msg}")
        print(f"    Run: gh auth login --hostname {host}")
        ok = False
    return ok


# ---------------------------------------------------------------------------
# Config IO
# ---------------------------------------------------------------------------

def load_existing() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Copilot-powered prompt elaboration
#
# Same pattern as auto_review.py: write the long prompt to a file in a
# scratch dir, hand `copilot` a short instruction pointing at it, and
# have the model write its JSON output to a sibling file. No GitHub I/O,
# no tools beyond filesystem in the scratch dir.
# ---------------------------------------------------------------------------

_ELABORATION_INSTRUCTIONS = """\
You are helping a user customize the prompt for an automated PR
code-reviewer. The user has typed __KIND_DESCRIPTION__ in shorthand
(sometimes just a single word like "efficiency" or "syntax", sometimes a
short phrase). For each shorthand item, expand it into ONE concrete,
actionable bullet of 1-3 sentences that an experienced senior engineer
would actually want a reviewer to follow on every PR.

Codebase context (use it to tailor the bullets to the actual stack /
domain, NOT to copy verbatim):
  __CODEBASE_DESCRIPTION__

For each bullet:
  - Name WHAT to look for (specific patterns, anti-patterns, smells).
  - Say WHY it matters in this codebase if the stack makes a difference.
  - __KIND_RULES__
  - Stay under ~50 words per bullet. Be precise, not preachy.
  - Do NOT add bullets the user did not ask for.
  - Do NOT restate the built-in defaults (correctness, security, perf,
    architecture, dependency hygiene are already covered by the base
    prompt). Your job is to elaborate the user's items, in their order.

User's shorthand items (one per line):
__RAW_ITEMS__

Write your answer as a JSON object to `elaborated.json` in the current
working directory. Do not print to stdout. Schema:

{
  "items": [
    "<elaborated bullet for shorthand item 1>",
    "<elaborated bullet for shorthand item 2>",
    ...
  ]
}

The `items` array MUST have exactly the same length as the user's input
and be in the same order. After writing the file, your job is done.
"""

_KIND_META = {
    "focus": {
        "description": "things the reviewer should ALWAYS look for",
        "rules": (
            "Phrase as something to flag / catch / question, not as praise."
        ),
    },
    "avoid": {
        "description": "kinds of comments the reviewer should NEVER make",
        "rules": (
            "Phrase as 'Do NOT comment on X' or 'Skip Y'. Be explicit about "
            "the boundary so the reviewer doesn't accidentally still flag it."
        ),
    },
    "style": {
        "description": "the reviewer's tone / voice / style preferences",
        "rules": (
            "Treat the user's input as ONE item even if multi-line. Output "
            "ONE bullet, not several. Translate the vibe into concrete "
            "drafting rules ('use second person', 'lead with the fix', "
            "'avoid hedging words like maybe/consider', etc.)."
        ),
    },
}


def elaborate_items(raw_items: list[str], kind: str, codebase: str) -> list[str] | None:
    """Ask Copilot to expand shorthand into reviewer guidance bullets.

    Returns the elaborated list on success, or None on any failure (caller
    should fall back to the raw input — elaboration must NEVER block setup).
    """
    if not raw_items:
        return []
    if not shutil.which("copilot"):
        print("  (skipping elaboration: `copilot` CLI not on PATH)")
        return None
    meta = _KIND_META.get(kind, _KIND_META["focus"])

    tmp_dir = Path(tempfile.mkdtemp(prefix="autorev_setup_"))
    input_file = tmp_dir / "elaborate_input.md"
    output_file = tmp_dir / "elaborated.json"

    raw_block = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(raw_items))
    prompt = (
        _ELABORATION_INSTRUCTIONS
        .replace("__KIND_DESCRIPTION__", meta["description"])
        .replace("__KIND_RULES__", meta["rules"])
        .replace("__CODEBASE_DESCRIPTION__", codebase or "(not specified)")
        .replace("__RAW_ITEMS__", raw_block)
    )
    input_file.write_text(prompt, encoding="utf-8")

    short = (
        "Read elaborate_input.md in this directory and follow it exactly. "
        "Write the resulting JSON object to elaborated.json in this same "
        "directory. Do not print the JSON to stdout."
    )
    cmd = [
        "copilot",
        "--model", COPILOT_MODEL,
        "--effort", COPILOT_EFFORT,
        "--allow-all-tools",
        "--add-dir", str(tmp_dir),
        "--no-color",
        "-p", short,
    ]
    print(f"  Elaborating {len(raw_items)} {kind} item(s) via copilot "
          f"({COPILOT_MODEL}, effort={COPILOT_EFFORT}, ~30-90s)…")
    t0 = time.time()
    try:
        res = subprocess.run(
            cmd, cwd=str(tmp_dir), capture_output=True, text=True,
            timeout=COPILOT_TIMEOUT, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        print(f"  (elaboration timed out after {COPILOT_TIMEOUT}s; keeping raw items)")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    dur = time.time() - t0

    try:
        if res.returncode != 0:
            print(f"  (copilot exited {res.returncode}; keeping raw items)")
            return None
        if not output_file.exists():
            print("  (copilot did not write elaborated.json; keeping raw items)")
            return None
        obj = json.loads(output_file.read_text(encoding="utf-8"))
        items = obj.get("items")
        if (not isinstance(items, list)
                or len(items) != len(raw_items)
                or not all(isinstance(x, str) and x.strip() for x in items)):
            print("  (elaborated.json shape unexpected; keeping raw items)")
            return None
        print(f"  Elaboration done in {dur:.1f}s.")
        return [s.strip() for s in items]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _preview_elaboration(label: str, raw: list[str], elaborated: list[str]) -> None:
    print(f"\n  --- {label} (before -> after) ---")
    for r, e in zip(raw, elaborated):
        print(f"    [in ] {r}")
        # Wrap elaborated text to ~78 cols for readability.
        body = e.replace("\n", " ").strip()
        words = body.split()
        line = "    [out] "
        out_lines = []
        for w in words:
            if len(line) + len(w) + 1 > 78:
                out_lines.append(line)
                line = "          " + w
            else:
                line = (line + " " + w) if line.endswith(" ") is False and line.strip() else line + w
        out_lines.append(line)
        for ln in out_lines:
            print(ln)
        print()


def _maybe_elaborate(items: list[str], kind: str, codebase: str, *,
                     non_interactive: bool) -> list[str]:
    """Offer elaboration; on accept, replace items. On reject/failure, return raw."""
    if not items:
        return items
    # In non-interactive mode, only elaborate if asked via env var, to keep
    # headless reruns deterministic and fast.
    if non_interactive:
        if os.environ.get("AUTOREVIEW_ELABORATE_ON_SETUP", "").lower() not in ("1", "true", "yes"):
            return items
    else:
        if not _ask_yes_no(
            f"  Expand these {kind} items into detailed reviewer guidance using Copilot?",
            default=True, non_interactive=non_interactive,
        ):
            return items

    elaborated = elaborate_items(items, kind, codebase)
    if elaborated is None:
        return items

    if non_interactive:
        return elaborated

    _preview_elaboration(f"{kind} items", items, elaborated)
    if _ask_yes_no("  Accept the elaborated version?", default=True):
        return elaborated
    print("  (keeping your original raw items)")
    return items


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------

def collect_config(existing: dict[str, Any], *, non_interactive: bool) -> dict[str, Any]:
    cfg = dict(existing)

    _print_header("1) GitHub connection")
    host = _ask(
        "GitHub host (e.g. github.com, or your enterprise GHE host)",
        default=existing.get("gh_host", "microsoft.ghe.com"),
        required=True, non_interactive=non_interactive,
    )
    cfg["gh_host"] = host

    repo = _ask(
        "Repository to review (owner/name)",
        default=existing.get("repo"),
        required=True, non_interactive=non_interactive,
    )
    cfg["repo"] = repo

    viewer = _gh_viewer_login(host)
    if viewer:
        print(f"  Detected reviewer login on {host}: {viewer}")
        print("  (PRs are picked up via `review-requested:@me` — no need to configure.)")

    _print_header("2) Daily summary email")
    if viewer:
        rec_default = existing.get("report_recipient") or f"{viewer}@microsoft.com"
    else:
        rec_default = existing.get("report_recipient", "")
    recipient = _ask(
        "Email to receive the daily 07:00 summary (blank to skip)",
        default=rec_default, non_interactive=non_interactive,
    )
    if recipient:
        cfg["report_recipient"] = recipient
    elif "report_recipient" in cfg:
        cfg.pop("report_recipient")

    _print_header("3) Tell the reviewer about your codebase")
    print("""
This one sentence is injected into the reviewer prompt so the model has
real context about the product / stack it's reviewing. Be concrete —
think 'pitch the codebase to a senior engineer in one line'.

  Examples:
    - "a TypeScript / React / Node monorepo for the Copilot Studio agent designer"
    - "a Python Django app handling B2B invoice ingestion and OCR"
    - "a Go microservice that brokers messages between Kafka and PostgreSQL"
""".rstrip())
    codebase = _ask(
        "Codebase description (one sentence)",
        default=existing.get("codebase_description"),
        required=True, non_interactive=non_interactive,
    )
    cfg["codebase_description"] = codebase

    _print_header("4) What should the reviewer focus on?")
    print("""
List specific concerns this reviewer should ALWAYS look out for. These are
*on top of* the built-in defaults (correctness, security, performance,
architecture, dependency hygiene). One item per line, blank line to finish.

  SHORTHAND IS FINE — after you finish, Copilot can expand single words
  ("efficiency", "syntax", "concurrency") into detailed reviewer guidance
  using your codebase context. You'll get to preview and accept/reject.

  Examples (shorthand → what Copilot might produce):
    "efficiency"   →  "Flag O(n^2) loops over arrays that can be large,
                       missed memoization in hot React renders, N+1 fetches
                       where a batched call would do, and unnecessary
                       re-renders from new object/array literals in props."
    "syntax"       →  "Catch dead code, unreachable branches, swallowed
                       errors (empty catch / unhandled promise rejections),
                       and misuse of async/await (missing await, fire-and-
                       forget where the result is needed)."
    "concurrency"  →  "Flag shared mutable state without locking, awaits
                       inside loops that should be Promise.all, and any
                       new background timer that doesn't get cleared on
                       unmount/dispose."
""".rstrip())
    cfg["review_focus"] = _ask_list(
        "Focus areas:",
        defaults=existing.get("review_focus", []),
        non_interactive=non_interactive,
    )
    cfg["review_focus"] = _maybe_elaborate(
        cfg["review_focus"], "focus", codebase,
        non_interactive=non_interactive,
    )

    _print_header("5) What should the reviewer NEVER comment on?")
    print("""
Items the reviewer will be told to skip even if it notices them. Use this
to silence noise specific to your codebase (e.g. generated code, formatter
choices, things you have a linter for). One per line, blank line to finish.

  Examples:
    - "Storybook story file formatting"
    - "Generated GraphQL types in src/__generated__/"
    - "Translation file ordering"
""".rstrip())
    cfg["review_avoid"] = _ask_list(
        "Things to avoid:",
        defaults=existing.get("review_avoid", []),
        non_interactive=non_interactive,
    )
    cfg["review_avoid"] = _maybe_elaborate(
        cfg["review_avoid"], "avoid", codebase,
        non_interactive=non_interactive,
    )

    _print_header("6) Reviewer style / voice")
    print("""
Free-form prose appended to the reviewer prompt. Tell the model what tone
and depth you want. Skip if you're happy with the defaults.

  Examples:
    - "Be direct and terse like a senior eng. Never write 'consider X'
       without saying exactly what and why."
    - "Be supportive and explanatory — many authors are interns this
       summer. Show the fix, don't just point at the problem."
""".rstrip())
    style = _ask(
        "Reviewer style (blank to skip)",
        default=existing.get("reviewer_style", ""),
        non_interactive=non_interactive,
    )
    if style:
        elaborated = _maybe_elaborate([style], "style", codebase,
                                      non_interactive=non_interactive)
        cfg["reviewer_style"] = elaborated[0] if elaborated else style
    elif "reviewer_style" in cfg:
        cfg.pop("reviewer_style")

    return cfg


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

def offer_register_tasks(*, non_interactive: bool) -> None:
    if os.name != "nt":
        print("\nScheduled tasks are Windows-only; skipping (you can wire up cron yourself).")
        return
    _print_header("7) Windows Scheduled Tasks")
    print("""
Two tasks ship with this project:
  • AgenticAutomations-AutoReview   — runs every 5 min (auto_review.py)
  • AgenticAutomations-DailyReport  — runs Mon-Fri 07:00 (send_daily_report.py)

You can register them now, or run the .ps1 scripts manually later.
""".rstrip())

    if _ask_yes_no("Register the 5-min auto-review task NOW?", default=False,
                   non_interactive=non_interactive):
        live = _ask_yes_no("  Live mode (will POST reviews)?  No = dry-run.",
                           default=False, non_interactive=non_interactive)
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(SCRIPT_DIR / "register_scheduled_task.ps1")]
        if live:
            cmd.append("-Live")
        print(f"  Running: {' '.join(cmd)}")
        subprocess.run(cmd, check=False)

    if _ask_yes_no("Register the daily 07:00 report task NOW?", default=False,
                   non_interactive=non_interactive):
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(SCRIPT_DIR / "register_daily_report_task.ps1")]
        print(f"  Running: {' '.join(cmd)}")
        subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--non-interactive", action="store_true",
                    help="Skip every prompt; use existing config values or fail "
                         "on any required-without-default.")
    ap.add_argument("--skip-prereqs", action="store_true",
                    help="Don't run the prereq check section.")
    ap.add_argument("--elaborate", action="store_true",
                    help="Skip the interview; just re-run Copilot elaboration "
                         "on the focus/avoid/style items already in config.json. "
                         "Useful after editing config.json by hand.")
    args = ap.parse_args()

    print("Auto-Reviewer setup")
    print(f"  Config will be written to: {CONFIG_PATH}")
    print(f"  Example reference:         {EXAMPLE_PATH}")

    existing = load_existing()
    if existing:
        print(f"  (existing config detected — its values will be the prompt defaults)")

    if args.elaborate:
        if not existing:
            print("\nERROR: --elaborate requires an existing config.json to elaborate.",
                  file=sys.stderr)
            return 2
        codebase = existing.get("codebase_description", "")
        cfg = dict(existing)
        cfg["review_focus"] = _maybe_elaborate(
            existing.get("review_focus", []), "focus", codebase,
            non_interactive=False,
        )
        cfg["review_avoid"] = _maybe_elaborate(
            existing.get("review_avoid", []), "avoid", codebase,
            non_interactive=False,
        )
        style = existing.get("reviewer_style", "").strip()
        if style:
            elaborated = _maybe_elaborate([style], "style", codebase,
                                          non_interactive=False)
            if elaborated:
                cfg["reviewer_style"] = elaborated[0]
        write_config(cfg)
        print(f"\n✓ Wrote {CONFIG_PATH}")
        return 0

    host_for_check = existing.get("gh_host", "microsoft.ghe.com")
    if not args.skip_prereqs:
        ok = run_prereq_checks(host_for_check)
        if not ok and not args.non_interactive:
            if not _ask_yes_no("\nPrereqs failed. Continue anyway?", default=False):
                print("Aborting. Fix the prereqs above and re-run.")
                return 1

    try:
        cfg = collect_config(existing, non_interactive=args.non_interactive)
    except RuntimeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2

    write_config(cfg)
    print(f"\n✓ Wrote {CONFIG_PATH}")

    _print_header("Try it out")
    print("""
  1. Dry-run (no posts to GitHub, writes artifacts under reviews/):
       python auto_review.py --dry-run --verbose

  2. Single-PR dry-run:
       python auto_review.py --dry-run --only-pr <pr-number> --verbose

  3. Preview the daily report locally:
       python send_daily_report.py --dry-run --verbose

  4. Once you trust it, go live:
       python auto_review.py
""".rstrip())

    offer_register_tasks(non_interactive=args.non_interactive)

    return 0


if __name__ == "__main__":
    sys.exit(main())
