#!/usr/bin/env python3
"""
Auto-review PRs in microsoft.ghe.com/bic/agentic-automations where the
authenticated user is a requested reviewer.

Pipeline (per cycle, every ~20 min):
  1. Python lists open, non-draft PRs returned by
       `gh pr list --search "is:pr is:open review-requested:@me"`.
  2. For each PR the script consults state.json to decide one of:
       - skip       : already reviewed at this HEAD SHA, no new author
                      activity on a blocking review.
       - review     : never reviewed, or HEAD SHA changed since last
                      review (and last decision wasn't a block).
       - reconsider : previous decision was `request_changes`. Either
                      HEAD changed, or the author replied / pushed
                      commits / left new comments since our review was
                      submitted. The script gathers those replies and
                      asks Copilot whether the block should be lifted.
  3. For both `review` and `reconsider`, ONLY the reasoning task is
     delegated to `copilot -p` with a strict JSON contract. Python
     does all the GitHub I/O (listing PRs, fetching diff, finding our
     prior review, scanning for author replies, posting the new
     review).
  4. state.json records: head_sha, decision, our review id + submission
     time, and per-reconsideration history, so we never re-review the
     same SHA or re-reconsider with no new author activity.

Designed to be invoked every ~20 minutes by Task Scheduler.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GH_HOST = "microsoft.ghe.com"
DEFAULT_REPO = "bic/agentic-automations"

# Cap on diff size sent to the model (characters). Very large PRs are
# truncated so we still get an architecture-level review instead of
# blowing up the prompt.
MAX_DIFF_CHARS = 180_000
MAX_FILES_LISTED = 200

# Where state and logs live.
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "auto_review.log"
REVIEWS_DIR = SCRIPT_DIR / "reviews"
# Append-only ledger: one JSON record per review/reconsider, for later
# impact reporting ("how many issues caught, how many blocks, etc.").
METRICS_PATH = REVIEWS_DIR / "metrics.jsonl"

# Copilot model + reasoning effort. Architecture review is non-trivial.
COPILOT_MODEL = os.environ.get("COPILOT_REVIEW_MODEL", "claude-opus-4.7-1m-internal")
COPILOT_EFFORT = os.environ.get("COPILOT_REVIEW_EFFORT", "high")

# Hard wall-clock cap for the Copilot review call (seconds).
COPILOT_TIMEOUT = int(os.environ.get("COPILOT_REVIEW_TIMEOUT", "900"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(message)s"
    # Windows console default is cp1252; force UTF-8 so emoji/ANSI from
    # subprocess output we log doesn't crash the handler.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


# ---------------------------------------------------------------------------
# State (so we don't re-review the same SHA every 20 minutes)
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("state.json unreadable; starting fresh")
    return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------------

def _gh_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GH_HOST"] = GH_HOST
    # Make output predictable.
    env["NO_COLOR"] = "1"
    return env


def gh(args: list[str], *, check: bool = True, input_text: str | None = None,
       timeout: int = 120) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    logging.debug("gh %s", " ".join(args))
    res = subprocess.run(
        cmd, env=_gh_env(), capture_output=True, text=True,
        input=input_text, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"gh failed ({res.returncode}): {' '.join(args)}\nSTDERR: {res.stderr.strip()}"
        )
    return res


# ---------------------------------------------------------------------------
# PR data model
# ---------------------------------------------------------------------------

@dataclass
class PullRequest:
    number: int
    title: str
    author: str
    head_sha: str
    base_ref: str
    head_ref: str
    is_draft: bool
    body: str
    url: str
    files: list[dict[str, Any]] = field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False


def list_open_prs(repo: str) -> list[PullRequest]:
    """List open, non-draft PRs where the authenticated user is a
    requested reviewer."""
    prs: list[PullRequest] = []
    fields = "number,title,author,headRefOid,baseRefName,headRefName,isDraft,body,url"
    res = gh([
        "pr", "list",
        "--repo", repo,
        "--search", "is:pr is:open review-requested:@me",
        "--limit", "200",
        "--json", fields,
    ])
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        logging.error("Failed to parse PR list")
        return prs
    for p in data:
        if p.get("isDraft"):
            continue
        prs.append(PullRequest(
            number=p["number"],
            title=p.get("title", ""),
            author=(p.get("author") or {}).get("login", "?"),
            head_sha=p.get("headRefOid", ""),
            base_ref=p.get("baseRefName", ""),
            head_ref=p.get("headRefName", ""),
            is_draft=bool(p.get("isDraft")),
            body=p.get("body") or "",
            url=p.get("url", ""),
        ))
    prs.sort(key=lambda x: x.number)
    return prs


def _build_diff_from_files_api(repo: str, pr_number: int) -> tuple[str, bool]:
    """Fallback when `gh pr diff` rejects the PR (HTTP 406: >300 files).

    Uses the Files API (paginated) and reconstructs a unified diff from each
    file's `patch`. Returns (diff_text, truncated_flag). `truncated_flag` is
    True when we hit MAX_DIFF_CHARS while concatenating.
    """
    res = gh(
        ["api", "--paginate", f"repos/{repo}/pulls/{pr_number}/files?per_page=100"],
        timeout=180,
    )
    try:
        # --paginate concatenates JSON arrays; gh returns one merged array.
        files = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        files = []
    chunks: list[str] = []
    total = 0
    truncated = False
    for f in files:
        patch = f.get("patch")
        filename = f.get("filename", "<unknown>")
        if not patch:
            # Binary, renamed-only, or omitted by API. Still record the file.
            header = f"diff --git a/{filename} b/{filename}\n(no patch available: status={f.get('status')}, +{f.get('additions',0)}/-{f.get('deletions',0)})\n"
            chunk = header
        else:
            chunk = (
                f"diff --git a/{filename} b/{filename}\n"
                f"--- a/{filename}\n+++ b/{filename}\n"
                f"{patch}\n"
            )
        if total + len(chunk) > MAX_DIFF_CHARS:
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return "".join(chunks), truncated


def hydrate_pr(repo: str, pr: PullRequest) -> None:
    """Attach file list and unified diff to a PR."""
    # Files list (path + additions/deletions). Cap for prompt size.
    res = gh([
        "pr", "view", str(pr.number),
        "--repo", repo,
        "--json", "files",
    ])
    try:
        files = (json.loads(res.stdout or "{}") or {}).get("files") or []
    except json.JSONDecodeError:
        files = []
    pr.files = files[:MAX_FILES_LISTED]

    # Unified diff. Fall back to Files API when the PR exceeds the 300-file
    # `gh pr diff` cap (HTTP 406).
    try:
        diff_res = gh(["pr", "diff", str(pr.number), "--repo", repo], timeout=180)
        diff = diff_res.stdout or ""
        if len(diff) > MAX_DIFF_CHARS:
            pr.diff = diff[:MAX_DIFF_CHARS]
            pr.diff_truncated = True
        else:
            pr.diff = diff
            pr.diff_truncated = False
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 406" in msg or "exceeded the maximum number of files" in msg:
            logging.warning(
                "PR #%d diff too large for `gh pr diff`; falling back to Files API",
                pr.number,
            )
            diff_text, truncated = _build_diff_from_files_api(repo, pr.number)
            pr.diff = diff_text
            pr.diff_truncated = truncated or True  # mega-PR: always flag
        else:
            raise


# ---------------------------------------------------------------------------
# Copilot review (the ONLY task we delegate to the model)
# ---------------------------------------------------------------------------

REVIEW_INSTRUCTIONS = """\
You are an automated code reviewer for a TypeScript / React / Node codebase
(microsoft.ghe.com/bic/agentic-automations -- the Copilot Studio agentic
automations product). You will be given a single pull request: its title,
description, file list, and unified diff. You will NOT browse the repo,
run tools, or fetch anything else. Review ONLY what is provided.

You MUST write your response as a JSON object to a file named
`review_output.json` in the current working directory. Do not print the
JSON to stdout. After writing the file, your job is done. The JSON must
match this schema:

{
  "decision": "approve" | "request_changes",
  "summary": "<3-8 sentence overall review covering correctness, architecture, and risk>",
  "comments": [
    {
      "file": "<path relative to repo root, MUST appear in the diff>",
      "line": <integer line number from the RIGHT side of the diff (the new file). Pick the most relevant added/context line within the hunk where the issue lives.>,
      "severity": "required" | "optional",
      "body": "<actionable, file/line-specific comment>"
    }
  ]
}

`severity` per comment:
  - "required" = must be addressed before merge. Correctness bugs, data
    loss, security issues, breaking contracts, missing error handling that
    will cause crashes, architectural violations that will compound. If
    your overall decision is "request_changes", at least one comment MUST
    be "required" (otherwise the block has no teeth).
  - "optional" = nit, style, micro-perf, defensive suggestion, "consider
    extracting", refactor idea, or anything you'd be fine merging without.
    Authors should feel free to ignore optional comments.

Be honest about the split. Don't mark everything required (that's noise).
Don't mark everything optional either — if something is actually wrong,
say so plainly.

PREFER inline comments tied to a specific file + line. Every issue you raise
should land on the most relevant added or context line in the new file
(right side of the diff). Use the `+nnn` / context line numbers visible in
the `@@ -a,b +c,d @@` hunk headers to pick the line. Only fall back to a
file-level comment (omit `line`) if the issue genuinely doesn't have a
single line to anchor to (e.g. "this whole file should not exist").

Review priorities (in order):
  1. Correctness: does the change actually solve the stated problem? Look
     for off-by-one, wrong control flow, missing await, swallowed errors,
     race conditions, broken contracts with callers.
  2. Architecture / design soundness: is this the right layer? Does it
     respect existing abstractions, or shoehorn logic where it doesn't
     belong? Will it cause coupling, duplication, or fragile invariants?
  3. Reuse over reinvention: if the diff introduces a utility, hook,
     reducer, telemetry helper, retry wrapper, fetch wrapper, debounce,
     deep-clone, type guard, etc. that looks generic, FLAG IT and ask
     the author to confirm there isn't already an equivalent in a
     shared package (e.g. `packages/common`, `packages/shared`, an
     existing `utils/`, a fluentui/lodash primitive). Name the likely
     existing home if you can guess from imports / naming patterns in
     the diff. If there's a clearly-named duplicate already imported
     elsewhere in the same diff, call it out as a hard duplication.
  4. Performance: O(n²) loops over arrays that can be large, repeated
     work that should be memoized, blocking the event loop with sync
     I/O or heavy CPU, unnecessary re-renders (React: missing
     `useMemo`/`useCallback`, new object/array literals in props,
     unstable refs, context value churn), wasteful network round-trips
     (N+1 fetches, missing batching/dedup), large bundles introduced
     by importing whole libraries when a single helper would do.
  5. Circular references / cyclic dependencies: `import A` ↔ `import B`
     between modules (often shows up as undefined-at-import-time), and
     reference cycles in long-lived objects (parent ↔ child holders,
     subscriptions never torn down, event-listener closures pinning
     state) that risk memory leaks. Call out both the cyclic import
     pair and what to do (lift a shared type to a third module, invert
     the dependency, weak-ref / explicit dispose).
  6. Failure modes & edge cases: empty/undefined inputs, network failure,
     concurrent calls, tenant/locale boundaries, telemetry impact.
  7. Security & privacy: leaked secrets, PII, missing authz, unsafe
     deserialization, XSS, prototype pollution.
  8. Test coverage proportional to risk -- not "add a test" boilerplate.

Decision rubric (BINARY — bias toward APPROVE):
  - "approve" is the DEFAULT. Approve when:
      * the change works and isn't breaking anything, OR
      * the only issues you found are minor (style, nits, defensive
        cleanups, "consider refactoring", "this could be more readable",
        small perf wins, suggested rename, missing JSDoc, etc.).
    You may still leave 0..many comments under an approve — tag each
    with severity="optional" so the author knows they're not blocking.

  - "request_changes" ONLY for material problems, specifically:
      * a real functional bug (wrong control flow, wrong API contract,
        null-deref, race, off-by-one with user-visible impact)
      * a regression that breaks existing behavior or callers
      * a security issue (auth bypass, injection, secret leak, unsafe
        deserialization, missing validation on a trust boundary)
      * data loss or data corruption (writes the wrong thing, mutates
        shared state unsafely, silently drops events)
      * an architectural violation that will cause real harm if shipped
        (e.g. circular dependency that breaks the build / tree-shake;
        leaking a layer concern across a hard boundary; duplicating
        infrastructure that already exists and creating a fork)
      * a clear performance regression that will be felt at runtime
        (NOT micro-perf or theoretical concerns)
    When you block, at least one comment MUST be severity="required"
    and must concretely describe the problem and the expected fix.

Things that are NEVER reasons to request changes (leave as optional
comments under an approve instead):
  - naming, formatting, comments, JSDoc, log strings
  - "this could be cleaner / extracted / split / inlined"
  - speculative perf concerns you can't quantify
  - missing tests for code paths that already worked
  - suspected duplication you can't prove from the diff (ask the author
    as an optional question)
  - style preferences, opinions about React/TS idioms
  - one-off typos in non-user-facing strings

If you're on the fence — approve. The author can read your optional
comments and decide. Blocking should require a concrete, articulable,
material problem.

Strict rules for comments:
  - Be specific. Reference the file and what the code does. No "consider
    extracting this" without saying what and why.
  - Do NOT comment on formatting, whitespace, import order, trivial naming,
    minor wording in strings, or anything a linter/Prettier handles.
  - Do NOT pile on. 0-6 comments is normal; only exceed that for genuinely
    large or risky PRs.
  - If the diff was truncated, say so in the summary and scope the review
    to what you saw.
  - For suspected duplication you can't fully prove from the diff, phrase
    the comment as a question to the author ("Does `packages/common/...`
    already expose something like this? If so, please reuse it; if not,
    consider lifting this there.") and mark it severity="optional" — never
    block on suspicion alone.
  - Never invent code that is not in the diff.

Write the JSON object to `review_output.json` now. Default to approve
unless you have a concrete, material problem to point at.
"""


def build_review_prompt(pr: PullRequest) -> str:
    files_str = "\n".join(
        f"  - {f.get('path','?')} (+{f.get('additions',0)} -{f.get('deletions',0)})"
        for f in pr.files
    ) or "  (no file metadata)"
    body = (pr.body or "").strip() or "(no description)"
    trunc_note = ""
    if pr.diff_truncated:
        trunc_note = (
            f"\n\n[NOTE] The unified diff was truncated to {MAX_DIFF_CHARS} characters."
            " Review what you see and call out the truncation in your summary."
        )
    return (
        REVIEW_INSTRUCTIONS
        + "\n\n=== PULL REQUEST ===\n"
        + f"Repo: {DEFAULT_REPO}\n"
        + f"PR #{pr.number}: {pr.title}\n"
        + f"Author: {pr.author}\n"
        + f"Branch: {pr.head_ref} -> {pr.base_ref}\n"
        + f"URL: {pr.url}\n\n"
        + "--- Description ---\n"
        + body
        + "\n\n--- Changed files ---\n"
        + files_str
        + trunc_note
        + "\n\n--- Unified diff ---\n"
        + pr.diff
        + "\n=== END PULL REQUEST ===\n"
        + "\nReturn the JSON object now."
    )


def run_copilot_review(prompt: str) -> dict[str, Any]:
    """Initial review path."""
    return run_copilot_review_call(prompt, validator=_validate_review)


def run_copilot_reconsider(prompt: str) -> dict[str, Any]:
    """Reconsideration path."""
    return run_copilot_review_call(prompt, validator=_validate_reconsider)


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_review_json(text: str, validator=None) -> dict[str, Any]:
    """Tolerantly extract the JSON object Copilot returned."""
    if validator is None:
        validator = _validate_review
    if not text:
        raise ValueError("empty copilot output")
    # Strip ``` fences if any.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return validator(json.loads(cleaned))
    except json.JSONDecodeError:
        pass
    # Fall back: find the largest {...} block.
    matches = _JSON_OBJ_RE.findall(text)
    for m in sorted(matches, key=len, reverse=True):
        try:
            return validator(json.loads(m))
        except json.JSONDecodeError:
            continue
    raise ValueError(f"could not parse JSON from copilot output: {text[:300]!r}")


def _validate_review(obj: Any, *, allow_comment: bool = False) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("review must be a JSON object")
    decision = obj.get("decision")
    # Initial reviews are binary (approve / request_changes). Reconsiders
    # additionally allow "comment" to drop a prior block without
    # endorsing the PR (used when the author gave a rationale we don't
    # fully agree with but no longer want to block on).
    valid: tuple[str, ...]
    if allow_comment:
        valid = ("approve", "request_changes", "comment")
    else:
        valid = ("approve", "request_changes")
        # Legacy coercion for initial reviews only.
        if decision == "comment":
            logging.info("Model returned legacy 'comment' verdict on initial review; coercing to 'approve'.")
            decision = "approve"
    if decision not in valid:
        raise ValueError(f"invalid decision: {decision!r}")
    obj["decision"] = decision
    summary = (obj.get("summary") or "").strip()
    if not summary:
        raise ValueError("missing summary")
    comments = obj.get("comments") or []
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    norm: list[dict[str, Any]] = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = (c.get("body") or "").strip()
        if not body:
            continue
        sev = (c.get("severity") or "").strip().lower()
        if sev not in ("required", "optional"):
            sev = "required" if decision == "request_changes" else "optional"
        entry: dict[str, Any] = {
            "file": (c.get("file") or "").strip(),
            "body": body,
            "severity": sev,
        }
        ln = c.get("line")
        if isinstance(ln, int) and ln > 0:
            entry["line"] = ln
        elif isinstance(ln, str) and ln.strip().isdigit():
            entry["line"] = int(ln.strip())
        norm.append(entry)
    return {"decision": decision, "summary": summary, "comments": norm}


def _validate_reconsider_NEW_REMOVED(obj: Any) -> dict[str, Any]:
    """Removed — see _validate_reconsider near line 1095, which is the
    real one. Kept as a NotImplementedError stub so a future drift loud-
    fails instead of silently shadowing."""
    raise NotImplementedError("use _validate_reconsider() near line 1095")


# ---------------------------------------------------------------------------
# Posting the review back to GitHub
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff_right_lines(diff: str) -> dict[str, set[int]]:
    """Return {path: set(right-side line numbers commentable inline)}.

    GitHub will only accept inline review comments on lines that appear
    in the diff hunk (additions or context). We map them strictly so we
    can drop hallucinated coordinates before they get the API to 422.
    """
    valid: dict[str, set[int]] = {}
    current: str | None = None
    right: int | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current = None
            right = None
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if p == "/dev/null":
                current = None
            else:
                current = p[2:] if p.startswith("b/") else p
                valid.setdefault(current, set())
            right = None
            continue
        m = _HUNK_RE.match(raw)
        if m:
            right = int(m.group(1))
            continue
        if current is None or right is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            valid[current].add(right)
            right += 1
        elif raw.startswith(" "):
            valid[current].add(right)
            right += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            pass  # left-only or "\ No newline" marker
    return valid


_SEVERITY_PREFIX = {
    "required": "**🔴 Required:** ",
    "optional": "**🟡 Optional:** ",
}


def _decorate_body(c: dict[str, Any]) -> str:
    """Prepend a Required / Optional badge to the comment body."""
    body = c.get("body", "") or ""
    prefix = _SEVERITY_PREFIX.get(c.get("severity", "required"), _SEVERITY_PREFIX["required"])
    return prefix + body


def _split_inline_vs_general(review: dict[str, Any],
                             valid: dict[str, set[int]]) -> tuple[list[dict], list[dict]]:
    """Map model comments to inline review comments + general fallbacks."""
    inline: list[dict[str, Any]] = []
    general: list[dict[str, Any]] = []
    for c in review.get("comments", []):
        f = (c.get("file") or "").strip()
        ln = c.get("line")
        body = _decorate_body(c)
        if f and isinstance(ln, int) and ln in valid.get(f, set()):
            inline.append({"path": f, "line": ln, "side": "RIGHT", "body": body})
        else:
            general.append({"file": f, "body": body, "line": ln})
    return inline, general


def _verdict_header(decision: str) -> str:
    return {
        "approve":         "## ✅ Verdict: **APPROVE**\n\n",
        "request_changes": "## 🛑 Verdict: **REQUEST CHANGES**\n\n",
        "comment":         "## 💬 Verdict: **COMMENT**\n\n",
    }[decision]


def format_review_body(pr: PullRequest, review: dict[str, Any],
                       general_comments: list[dict[str, Any]] | None = None) -> str:
    parts = [
        _verdict_header(review["decision"]),
        f"_🤖 Automated review · model: `{COPILOT_MODEL}` · effort: "
        f"`{COPILOT_EFFORT}` · HEAD: `{pr.head_sha[:10]}`_\n\n",
        "### Summary\n",
        review["summary"].rstrip() + "\n",
    ]
    if general_comments:
        parts.append("\n### Additional notes\n")
        for c in general_comments:
            loc = f"`{c['file']}`" if c.get("file") else "(general)"
            parts.append(f"- **{loc}** — {c['body']}\n")
    if pr.diff_truncated:
        parts.append(
            f"\n> ⚠ Diff was truncated to {MAX_DIFF_CHARS} chars; review is scoped accordingly.\n"
        )
    return "".join(parts)


def format_reconsider_body(pr: PullRequest, review: dict[str, Any],
                           prior_decision: str,
                           general_comments: list[dict[str, Any]] | None = None) -> str:
    transition = f"_Prior decision: `{prior_decision}` → now: **{review['decision']}**_\n\n"
    parts = [
        _verdict_header(review["decision"]),
        transition,
        f"_🤖 Automated re-review · model: `{COPILOT_MODEL}` · effort: "
        f"`{COPILOT_EFFORT}` · HEAD: `{pr.head_sha[:10]}`_\n\n",
        "### Summary\n",
        review["summary"].rstrip() + "\n",
    ]
    if review.get("remaining_concerns"):
        parts.append("\n### Remaining concerns\n")
        for r in review["remaining_concerns"]:
            parts.append(f"- {r}\n")
    if general_comments:
        parts.append("\n### Additional notes\n")
        for c in general_comments:
            loc = f"`{c['file']}`" if c.get("file") else "(general)"
            parts.append(f"- **{loc}** — {c['body']}\n")
    return "".join(parts)


def submit_review_via_api(repo: str, pr: PullRequest, review: dict[str, Any],
                          body: str) -> str | None:
    """POST a review with inline comments + general body via GH REST API.

    Maps the model's `(file, line)` issues to RIGHT-side inline comments
    on hunks that actually appear in the diff; unmappable entries are
    folded back into the body's "Additional notes" section by the caller.
    """
    event_map = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }
    valid = parse_diff_right_lines(pr.diff)
    inline, _ = _split_inline_vs_general(review, valid)

    payload = {
        "commit_id": pr.head_sha,
        "event": event_map[review["decision"]],
        "body": body,
        "comments": inline,
    }
    res = gh(
        ["api", f"repos/{repo}/pulls/{pr.number}/reviews",
         "-X", "POST", "--input", "-"],
        input_text=json.dumps(payload),
        check=False,
    )
    if res.returncode != 0:
        # Inline comments occasionally trip 422 on awkward hunks (e.g.
        # binary-ish files). Retry without inline so we never lose the
        # review.
        logging.warning("inline review POST failed (%s): %s — retrying body-only",
                        res.returncode, res.stderr.strip()[:300])
        payload["comments"] = []
        # Re-render body with the inline issues folded into "Additional notes".
        # Caller built `body` for the inline-success case, so do this cheaply.
        gh(["api", f"repos/{repo}/pulls/{pr.number}/reviews",
            "-X", "POST", "--input", "-"],
           input_text=json.dumps(payload))
    return find_my_latest_review_id(repo, pr.number)


def post_review(repo: str, pr: PullRequest, review: dict[str, Any]) -> str | None:
    """Post the initial review with inline comments + verdict header."""
    valid = parse_diff_right_lines(pr.diff)
    inline, general = _split_inline_vs_general(review, valid)
    body = format_review_body(pr, review, general_comments=general)
    return submit_review_via_api(repo, pr, review, body)


_VIEWER_LOGIN: str | None = None


def get_viewer_login() -> str:
    """Resolve the authenticated GHE login (cached for the process)."""
    global _VIEWER_LOGIN
    if _VIEWER_LOGIN:
        return _VIEWER_LOGIN
    res = gh(["api", "user", "--jq", ".login"])
    _VIEWER_LOGIN = (res.stdout or "").strip()
    if not _VIEWER_LOGIN:
        raise RuntimeError("could not resolve gh viewer login")
    return _VIEWER_LOGIN


def find_my_latest_review_id(repo: str, pr_number: int) -> str | None:
    """Return the id of my most recently-submitted review on this PR."""
    me = get_viewer_login()
    owner, name = repo.split("/", 1)
    try:
        res = gh([
            "api", f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
            "--paginate",
        ])
        reviews = json.loads(res.stdout or "[]")
    except Exception:
        return None
    mine = [r for r in reviews
            if ((r.get("user") or {}).get("login") == me)
            and r.get("submitted_at")]
    if not mine:
        return None
    mine.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    rid = mine[0].get("id")
    return str(rid) if rid is not None else None


def find_my_latest_blocking_review_id(repo: str, pr_number: int) -> str | None:
    """Return the id of my most recently-submitted CHANGES_REQUESTED
    review on this PR, if it's still active (not already DISMISSED)."""
    me = get_viewer_login()
    owner, name = repo.split("/", 1)
    try:
        res = gh([
            "api", f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
            "--paginate",
        ])
        reviews = json.loads(res.stdout or "[]")
    except Exception:
        return None
    mine_blocking = [
        r for r in reviews
        if ((r.get("user") or {}).get("login") == me)
        and r.get("state") == "CHANGES_REQUESTED"
        and r.get("submitted_at")
    ]
    if not mine_blocking:
        return None
    mine_blocking.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    rid = mine_blocking[0].get("id")
    return str(rid) if rid is not None else None


def dismiss_review(repo: str, pr_number: int, review_id: str,
                   message: str) -> bool:
    """PUT /repos/.../pulls/{n}/reviews/{id}/dismissals to release a prior
    CHANGES_REQUESTED block. Returns True on success."""
    owner, name = repo.split("/", 1)
    payload = {"message": message, "event": "DISMISS"}
    try:
        gh([
            "api", f"repos/{owner}/{name}/pulls/{pr_number}/reviews/{review_id}/dismissals",
            "-X", "PUT", "--input", "-",
        ], input_text=json.dumps(payload))
        return True
    except Exception as exc:
        logging.warning("Failed to dismiss review %s on PR #%s: %s",
                        review_id, pr_number, exc)
        return False


def fetch_pr_activity_since(repo: str, pr_number: int, since_iso: str
                            ) -> dict[str, Any]:
    """Return everything potentially relevant since `since_iso`:
      - issue comments (PR conversation tab) authored by anyone except us
      - review comments (inline) authored by anyone except us
      - new commits pushed after since_iso
      - explicit re-review requests targeting us, created after since_iso
    The caller decides whether this is enough to trigger reconsideration.
    """
    me = get_viewer_login()
    owner, name = repo.split("/", 1)
    out: dict[str, Any] = {
        "issue_comments": [],
        "review_comments": [],
        "new_commits": [],
        "rerequests": [],
    }

    def _gh_json(args: list[str]) -> Any:
        try:
            return json.loads(gh(args).stdout or "[]")
        except Exception as e:
            logging.warning("activity fetch failed (%s): %s", args, e)
            return []

    issue_comments = _gh_json([
        "api", f"repos/{owner}/{name}/issues/{pr_number}/comments",
        "--paginate", "-X", "GET",
        "-f", f"since={since_iso}",
    ])
    for c in issue_comments:
        if (c.get("user") or {}).get("login") == me:
            continue
        out["issue_comments"].append({
            "author": (c.get("user") or {}).get("login", "?"),
            "created_at": c.get("created_at"),
            "body": (c.get("body") or "").strip(),
            "url": c.get("html_url"),
        })

    # Review comments don't accept `since` server-side; filter client-side.
    review_comments = _gh_json([
        "api", f"repos/{owner}/{name}/pulls/{pr_number}/comments",
        "--paginate",
    ])
    for c in review_comments:
        if (c.get("user") or {}).get("login") == me:
            continue
        if (c.get("created_at") or "") <= since_iso:
            continue
        out["review_comments"].append({
            "author": (c.get("user") or {}).get("login", "?"),
            "created_at": c.get("created_at"),
            "file": c.get("path"),
            "body": (c.get("body") or "").strip(),
            "in_reply_to_id": c.get("in_reply_to_id"),
            "url": c.get("html_url"),
        })

    commits = _gh_json([
        "api", f"repos/{owner}/{name}/pulls/{pr_number}/commits",
        "--paginate",
    ])
    for c in commits:
        committed_at = (((c.get("commit") or {}).get("committer") or {})
                        .get("date") or "")
        if committed_at and committed_at > since_iso:
            out["new_commits"].append({
                "sha": (c.get("sha") or "")[:10],
                "committed_at": committed_at,
                "message": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:200],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            })

    # Explicit re-review requests targeting me, via the issue timeline.
    # `review_requested` events have a `requested_reviewer.login`.
    timeline = _gh_json([
        "api", f"repos/{owner}/{name}/issues/{pr_number}/timeline",
        "--paginate",
        "-H", "Accept: application/vnd.github+json",
    ])
    for ev in timeline:
        if ev.get("event") != "review_requested":
            continue
        target = ((ev.get("requested_reviewer") or {}).get("login")
                  or (ev.get("requested_team") or {}).get("slug"))
        if target != me:
            continue
        when = ev.get("created_at") or ""
        if when and when > since_iso:
            out["rerequests"].append({
                "at": when,
                "by": ((ev.get("actor") or {}).get("login")) or "?",
            })

    return out


def activity_warrants_reconsider(activity: dict[str, Any]) -> bool:
    """Cheap heuristic so we don't burn Copilot calls on noise.

    A re-review request alone (with the same HEAD, no new author replies,
    no new commits) is NOT enough — the author already saw our last
    review and nothing has actually changed since. Re-answering would be
    spam. Re-requests count only when paired with new author activity or
    a HEAD bump (HEAD-changed is checked separately by the caller).
    """
    if activity.get("new_commits"):
        return True
    if activity.get("issue_comments"):
        return True
    for rc in activity.get("review_comments", []):
        if rc.get("in_reply_to_id") is not None or rc.get("body"):
            return True
    # rerequests is intentionally NOT a standalone trigger anymore.
    return False


RECONSIDER_INSTRUCTIONS = """\
You previously reviewed this pull request. Since then at least one of the
following has happened: the author pushed new commits, the author replied
to your review (issue comments or inline review comments), or you were
re-requested as a reviewer. Your job is to re-evaluate the PR in its
current state and decide what your review should now be.

You will be given:
  - The PR metadata.
  - Your prior review (decision, summary, comments).
  - All new activity since your prior action: author replies, new
    commits, and any explicit re-review requests.
  - The CURRENT unified diff. If HEAD has not changed, this is the same
    diff you reviewed; if HEAD changed, it reflects the latest state.

You will NOT browse the repo or fetch anything else. Reason ONLY from
what is provided.

You MUST write your response as a JSON object to a file named
`review_output.json` in the current working directory. Do not print the
JSON to stdout. The JSON must match this schema:

{
  "decision": "approve" | "request_changes" | "comment",
  "summary": "<3-8 sentences: what changed since your last review and why
              your decision stands or has changed>",
  "comments": [
    {
      "file": "<path that appears in the diff>",
      "line": <integer RIGHT-side line number from the current diff hunk>,
      "severity": "required" | "optional",
      "body": "<actionable, file/line-specific comment>"
    }
  ],
  "addresses_prior_block": true | false | null,
  "remaining_concerns": [
    "<short bullet — leave empty if decision is approve and nothing remains>"
  ]
}

PREFER inline comments anchored to a specific file + line on the RIGHT
side of the current diff. Only omit `line` if the concern genuinely has
no single anchor (rare).

`addresses_prior_block`:
  - true  if your prior decision was "request_changes" and the author has
          now resolved every blocking concern (via code change OR a
          convincing rationale).
  - false if your prior decision was "request_changes" and at least one
          blocking concern remains AND the author has not given a
          plausible rationale to drop it.
  - null  if your prior decision was not "request_changes" (so there was
          nothing to "address").

Decision rubric (THREE STATES — bias toward APPROVE; reserve
"request_changes" for material unresolved blockers):

  - "approve": the PR is in a mergeable state given the current diff and
    the author's responses. Surviving concerns are nits / style /
    "consider X" / micro-perf / missing doc — re-tag them
    severity="optional". This is the DEFAULT.

  - "comment": ONLY valid when your prior decision was "request_changes"
    AND the author has responded with a rationale that you DO NOT fully
    agree with, but which is plausible enough that you no longer want to
    block the PR. Use "comment" to drop the block while recording your
    disagreement. Quote the author's reply, explain concretely why you
    still have reservations, then defer to the author. Do not use
    "comment" if you actually agree with the author (use "approve") or
    if the rationale is empty / non-responsive / clearly wrong (use
    "request_changes").

  - "request_changes": a MATERIAL blocking concern (real functional bug,
    breaking change, security issue, data loss, harmful architectural
    violation, real perf regression) is unresolved AND the author has
    either not responded to it OR their response does not plausibly
    address it. Tag the blocking comment(s) severity="required" and
    concretely describe the problem and the fix. Do not keep the block
    alive just because nits remain.

State machine when prior decision was "request_changes":
  1. Read every author reply (inline + issue comments) carefully.
  2. For each blocking concern from your prior review, classify the
     author's response as: ADDRESSED (code change or convincing
     rationale), DISPUTED (rationale exists but you disagree),
     IGNORED (no response or non-responsive).
  3. If ALL blockers are ADDRESSED → "approve".
  4. If at least one is IGNORED OR clearly wrong → "request_changes".
  5. If the only remaining blockers are DISPUTED (author gave a
     plausible reason, you still disagree) → "comment" — drop the
     block, record disagreement, defer to the author.

Rules:
  - Engage with what the author actually said. Quote a short snippet of
    their reply when refuting or agreeing with it. Do not just restate
    your prior review.
  - Do not invent new nitpicks unrelated to the new activity unless the
    new commits introduced them.
  - If the diff was truncated, say so and scope accordingly.

Write the JSON object to `review_output.json` now.
"""


def build_reconsider_prompt(pr: PullRequest, prior: dict[str, Any],
                            activity: dict[str, Any],
                            prior_review_body_snippet: str) -> str:
    files_str = "\n".join(
        f"  - {f.get('path','?')} (+{f.get('additions',0)} -{f.get('deletions',0)})"
        for f in pr.files
    ) or "  (no file metadata)"
    body = (pr.body or "").strip() or "(no description)"

    prior_decision = prior.get("decision", "?")
    prior_reviewed_at = prior.get("reviewed_at_iso") or "?"
    prior_summary = prior.get("review_summary", "")
    prior_comments = prior.get("review_comments", [])
    head_changed = pr.head_sha != (prior.get("head_sha") or "")

    issue_comments = activity.get("issue_comments", [])
    review_comments = activity.get("review_comments", [])
    new_commits = activity.get("new_commits", [])

    def _fmt_list(items: list[str]) -> str:
        return "\n".join(items) if items else "  (none)"

    parts: list[str] = [
        RECONSIDER_INSTRUCTIONS,
        "\n\n=== PULL REQUEST ===\n",
        f"Repo: {DEFAULT_REPO}\n",
        f"PR #{pr.number}: {pr.title}\n",
        f"Author: {pr.author}\n",
        f"Branch: {pr.head_ref} -> {pr.base_ref}\n",
        f"URL: {pr.url}\n",
        f"HEAD changed since prior review: {head_changed}\n",
        "\n--- Description ---\n",
        body,
        "\n\n--- Changed files (current) ---\n",
        files_str,
        "\n\n=== YOUR PRIOR REVIEW ===\n",
        f"Decision: {prior_decision}\n",
        f"Submitted at: {prior_reviewed_at}\n",
        "Summary:\n",
        (prior_summary.strip() or "(unavailable)"),
        "\nComments:\n",
    ]
    if prior_comments:
        for c in prior_comments:
            parts.append(f"  - [{c.get('file') or 'general'}] {c.get('body','').strip()}\n")
    else:
        parts.append("  (none)\n")

    if prior_review_body_snippet:
        parts.append("\nPosted review body (rendered, abbreviated):\n")
        parts.append(prior_review_body_snippet[:4000])
        parts.append("\n")

    parts.append("\n=== NEW ACTIVITY SINCE YOUR PRIOR ACTION ===\n")
    rerequests = activity.get("rerequests", [])
    parts.append("\n-- Explicit re-review requests targeting you --\n")
    if rerequests:
        for r in rerequests:
            parts.append(f"  - [{r['at']}] requested by {r['by']}\n")
    else:
        parts.append("  (none)\n")
    parts.append("\n-- New commits --\n")
    parts.append(_fmt_list([
        f"  - {c['sha']} @ {c['committed_at']} by {c.get('author','?')}: {c['message']}"
        for c in new_commits
    ]))
    parts.append("\n\n-- Issue comments (PR conversation) --\n")
    if issue_comments:
        for c in issue_comments:
            parts.append(
                f"\n[{c['created_at']}] {c['author']}:\n{c['body']}\n"
            )
    else:
        parts.append("  (none)\n")

    parts.append("\n-- Inline review-thread comments --\n")
    if review_comments:
        for c in review_comments:
            reply_note = f" (reply to comment {c['in_reply_to_id']})" if c.get("in_reply_to_id") else ""
            parts.append(
                f"\n[{c['created_at']}] {c['author']} on `{c.get('file') or '?'}`{reply_note}:\n"
                f"{c['body']}\n"
            )
    else:
        parts.append("  (none)\n")

    trunc_note = ""
    if pr.diff_truncated:
        trunc_note = (
            f"\n\n[NOTE] Current diff was truncated to {MAX_DIFF_CHARS} chars."
            " Say so in your summary."
        )
    parts.append(trunc_note)
    parts.append("\n\n--- Current unified diff ---\n")
    parts.append(pr.diff)
    parts.append("\n=== END ===\n\nReturn the JSON object now.")
    return "".join(parts)


def _validate_reconsider(obj: Any) -> dict[str, Any]:
    base = _validate_review(obj, allow_comment=True)
    apb = obj.get("addresses_prior_block", None)
    if apb is None:
        base["addresses_prior_block"] = None
    else:
        base["addresses_prior_block"] = bool(apb)
    rc = obj.get("remaining_concerns") or []
    if not isinstance(rc, list):
        rc = []
    base["remaining_concerns"] = [str(x).strip() for x in rc if str(x).strip()]
    return base


def format_reconsider_body_LEGACY_REMOVED(pr, review, prior_decision):
    """Removed: superseded by format_reconsider_body at line 654 which
    supports general_comments + the new binary verdict. Stub kept only so
    line numbers stay stable for nearby references."""
    raise NotImplementedError("use format_reconsider_body() at line ~654")


def run_copilot_review_call(prompt: str,
                            validator=_validate_review) -> dict[str, Any]:
    """Invoke copilot; expect JSON in `review_output.json` in cwd."""
    if not shutil.which("copilot"):
        raise RuntimeError("`copilot` CLI not found on PATH")

    tmp_dir = Path(tempfile.mkdtemp(prefix="copilot_review_"))
    input_file = tmp_dir / "review_input.md"
    output_file = tmp_dir / "review_output.json"
    input_file.write_text(prompt, encoding="utf-8")

    short_prompt = (
        "Read the file review_input.md in this directory and follow the "
        "instructions inside it exactly. Write the resulting JSON object "
        "to a file named review_output.json in this same directory. "
        "Do not print the JSON to stdout."
    )

    try:
        cmd = [
            "copilot",
            "--model", COPILOT_MODEL,
            "--effort", COPILOT_EFFORT,
            "--allow-all-tools",
            "--add-dir", str(tmp_dir),
            "--no-color",
            "-p", short_prompt,
        ]
        logging.info(
            "Invoking copilot (model=%s, effort=%s, prompt=%d chars on disk)",
            COPILOT_MODEL, COPILOT_EFFORT, len(prompt),
        )
        t0 = time.time()
        res = subprocess.run(
            cmd, cwd=str(tmp_dir), capture_output=True, text=True,
            timeout=COPILOT_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
        dur = time.time() - t0
        logging.info(
            "copilot returned in %.1fs rc=%d (stdout=%d chars, output_file=%s)",
            dur, res.returncode, len(res.stdout or ""), output_file.exists(),
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"copilot exited {res.returncode}: {res.stderr.strip()[:500]}"
            )
        # Primary path: read the file Copilot wrote.
        if output_file.exists():
            try:
                obj = json.loads(output_file.read_text(encoding="utf-8"))
                return validator(obj)
            except Exception as e:
                logging.warning("review_output.json invalid, falling back to stdout parse: %s", e)
        # Fallback: try to fish JSON out of stdout (older behavior).
        return parse_review_json(res.stdout, validator=validator)
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


def save_review_artifact(pr: PullRequest, review: dict[str, Any]) -> Path:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"pr-{pr.number}-{pr.head_sha[:10]}.json"
    payload = {
        "pr": {
            "number": pr.number,
            "title": pr.title,
            "author": pr.author,
            "head_sha": pr.head_sha,
            "url": pr.url,
            "diff_truncated": pr.diff_truncated,
        },
        "review": review,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save_reconsider_artifact(pr: PullRequest, review: dict[str, Any]) -> Path:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = REVIEWS_DIR / f"pr-{pr.number}-{pr.head_sha[:10]}-reconsider-{ts}.json"
    path.write_text(json.dumps({
        "pr": {"number": pr.number, "title": pr.title, "head_sha": pr.head_sha,
               "url": pr.url, "diff_truncated": pr.diff_truncated},
        "review": review,
    }, indent=2), encoding="utf-8")
    return path


def append_metrics(pr: PullRequest, review: dict[str, Any], *,
                   kind: str, dry_run: bool,
                   prior_decision: str | None = None,
                   activity: dict[str, Any] | None = None,
                   artifact: Path | None = None) -> None:
    """Append one lean JSON record per review to the impact ledger.

    Stores counts and pointers only — full issue text lives in the
    per-PR artifact (`artifact_path`) so this file stays small enough to
    grep / load into pandas indefinitely. One line per review.
    """
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    comments = review.get("comments") or []
    record = {
        "at_iso": _iso_now(),
        "kind": kind,                          # "review" | "reconsider"
        "dry_run": dry_run,
        "pr_number": pr.number,
        "pr_author": pr.author,
        "pr_url": pr.url,
        "head_sha": pr.head_sha[:10],
        "decision": review.get("decision"),
        "issues_count": len(comments),
        "files_flagged": sorted({c.get("file") for c in comments if c.get("file")}),
        "artifact_path": artifact.name if artifact else None,
    }
    if kind == "reconsider":
        remaining = review.get("remaining_concerns") or []
        record["prior_decision"] = prior_decision
        record["addresses_prior_block"] = review.get("addresses_prior_block")
        record["remaining_concerns_count"] = len(remaining)
        record["block_lifted"] = (
            prior_decision == "request_changes"
            and review.get("decision") != "request_changes"
        )
        if activity:
            record["trigger"] = {
                "rerequests": len(activity.get("rerequests", [])),
                "issue_comments": len(activity.get("issue_comments", [])),
                "review_comments": len(activity.get("review_comments", [])),
                "new_commits": len(activity.get("new_commits", [])),
            }
    with METRICS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record_initial_review(state: dict[str, Any], pr: PullRequest,
                           review: dict[str, Any], review_id: str | None,
                           dry_run: bool) -> None:
    state[str(pr.number)] = {
        "head_sha": pr.head_sha,
        "decision": review["decision"],
        "review_id": review_id,
        "reviewed_at": int(time.time()),
        "reviewed_at_iso": _iso_now(),
        "review_summary": review["summary"],
        "review_comments": review["comments"],
        "dry_run": dry_run,
        "reconsiders": [],
    }


def _record_reconsider(state: dict[str, Any], pr: PullRequest,
                       review: dict[str, Any], review_id: str | None,
                       activity: dict[str, Any], dry_run: bool) -> None:
    entry = state.get(str(pr.number)) or {}
    history = entry.get("reconsiders") or []
    history.append({
        "at": int(time.time()),
        "at_iso": _iso_now(),
        "prior_decision": entry.get("decision"),
        "prior_head_sha": entry.get("head_sha"),
        "head_sha": pr.head_sha,
        "decision": review["decision"],
        "addresses_prior_block": review.get("addresses_prior_block"),
        "remaining_concerns": review.get("remaining_concerns", []),
        "review_id": review_id,
        "trigger": {
            "head_changed": pr.head_sha != entry.get("head_sha"),
            "rerequests": len(activity.get("rerequests", [])),
            "issue_comments": len(activity.get("issue_comments", [])),
            "review_comments": len(activity.get("review_comments", [])),
            "new_commits": len(activity.get("new_commits", [])),
        },
        "summary": review["summary"],
        "dry_run": dry_run,
    })
    entry["reconsiders"] = history
    # Effective current state moves forward.
    entry["decision"] = review["decision"]
    entry["head_sha"] = pr.head_sha
    state[str(pr.number)] = entry


def reconsider_pr(repo: str, pr: PullRequest, state: dict[str, Any],
                  dry_run: bool, forced: bool = False) -> str:
    """Re-evaluate a PR we've already reviewed. Trigger conditions:
       - HEAD SHA changed since prior action, OR
       - author replied / pushed commits since prior action, OR
       - someone re-requested us as a reviewer.
    Works for ANY prior decision (request_changes / comment / approve).
    """
    prev = state.get(str(pr.number)) or {}
    last_action_iso = prev.get("reviewed_at_iso")
    for rc in (prev.get("reconsiders") or []):
        if rc.get("at_iso") and rc["at_iso"] > (last_action_iso or ""):
            last_action_iso = rc["at_iso"]
    if not last_action_iso:
        return "skip-no-watermark"

    activity = fetch_pr_activity_since(repo, pr.number, last_action_iso)
    head_changed = pr.head_sha != prev.get("head_sha")
    prior_decision = (prev.get("decision") or "").lower()
    code_changed = head_changed or bool(activity.get("new_commits"))

    if not forced and not head_changed and not activity_warrants_reconsider(activity):
        logging.info("PR #%s: no new activity since %s; skipping reconsider",
                     pr.number, last_action_iso)
        return "skip-no-activity"

    # If we already approved this PR, only re-review when CODE has changed.
    # A reply or re-request alone shouldn't make us re-approve an
    # already-approved PR — there's nothing new to evaluate.
    if not forced and prior_decision == "approve" and not code_changed:
        logging.info(
            "PR #%s: already approved and no code changes since %s; skipping reconsider",
            pr.number, last_action_iso,
        )
        return "skip-already-approved-no-code-change"

    logging.info(
        "PR #%s: reconsidering (since %s) — head_changed=%s, rerequests=%d, "
        "issue=%d, inline=%d, new_commits=%d",
        pr.number, last_action_iso, head_changed,
        len(activity["rerequests"]),
        len(activity["issue_comments"]),
        len(activity["review_comments"]),
        len(activity["new_commits"]),
    )

    hydrate_pr(repo, pr)
    if not pr.diff.strip():
        return "skip-empty"

    prompt = build_reconsider_prompt(pr, prev, activity, prior_review_body_snippet="")
    review = run_copilot_reconsider(prompt)
    artifact = save_reconsider_artifact(pr, review)
    append_metrics(pr, review, kind="reconsider", dry_run=dry_run,
                   prior_decision=prev.get("decision"),
                   activity=activity, artifact=artifact)
    logging.info("Reconsider for PR #%s -> %s (addresses_prior_block=%s) saved %s",
                 pr.number, review["decision"],
                 review.get("addresses_prior_block"), artifact.name)

    # If no code changed AND the verdict is identical to the prior one,
    # there's nothing new to publish — re-posting the same summary on
    # the same code is noise. Skip the GitHub post but still record the
    # reconsider in state so the watermark advances and we don't keep
    # re-running on the same author activity.
    new_decision = (review.get("decision") or "").lower()
    if not forced and not code_changed and new_decision == prior_decision:
        logging.info(
            "PR #%s: verdict unchanged (%s) and no code changes; skipping post",
            pr.number, new_decision,
        )
        _record_reconsider(state, pr, review, review_id=None,
                           activity=activity, dry_run=True)
        return f"reconsidered-noop:{new_decision}"

    review_id: str | None = None
    if dry_run:
        print("\n" + "-" * 78)
        print(f"DRY-RUN RECONSIDER PR #{pr.number} by {pr.author}: {pr.title}")
        print(f"  prior:    {prev.get('decision')} at {prev.get('reviewed_at_iso')}")
        print(f"  trigger:  head_changed={head_changed} rerequests={len(activity['rerequests'])}"
              f" issue={len(activity['issue_comments'])} inline={len(activity['review_comments'])}"
              f" new_commits={len(activity['new_commits'])}")
        print(f"  decision: {review['decision']} (addresses_prior_block={review.get('addresses_prior_block')})")
        print(f"  remaining: {review.get('remaining_concerns')}")
        print(f"  summary : {review['summary'][:300]}")
        print(f"  url     : {pr.url}")
        print("-" * 78)
    else:
        valid = parse_diff_right_lines(pr.diff)
        inline, general = _split_inline_vs_general(review, valid)
        body = format_reconsider_body(pr, review,
                                      prior_decision=prev.get("decision","?"),
                                      general_comments=general)
        review_id = submit_review_via_api(repo, pr, review, body)
        logging.info("Posted reconsider %s on PR #%s (review_id=%s, inline=%d, general=%d)",
                     review["decision"], pr.number, review_id, len(inline), len(general))

        # If we just transitioned from request_changes -> comment, the
        # author gave a rationale we don't fully agree with but we no
        # longer want to block. A COMMENT review alone may not clear the
        # prior CHANGES_REQUESTED on branch-protection rules, so
        # explicitly dismiss our last blocking review.
        if (review["decision"] == "comment"
                and (prev.get("decision") or "").lower() == "request_changes"):
            blocking_id = find_my_latest_blocking_review_id(repo, pr.number)
            if blocking_id and blocking_id != str(review_id):
                ok = dismiss_review(
                    repo, pr.number, blocking_id,
                    message=("Dropping prior changes-requested in favor of a "
                             "comment-only review — author provided a rationale "
                             "I don't fully agree with but won't block on. "
                             "See follow-up review for details."),
                )
                logging.info("Dismissed prior blocking review %s on PR #%s: %s",
                             blocking_id, pr.number, "ok" if ok else "FAILED")

    _record_reconsider(state, pr, review, review_id, activity, dry_run)
    return f"reconsidered:{review['decision']}"


def review_pr(repo: str, pr: PullRequest, state: dict[str, Any],
              dry_run: bool, force: bool) -> str:
    """Dispatch: fresh review / reconsider / skip."""
    key = str(pr.number)
    prev = state.get(key) or {}

    if not prev:
        # Never reviewed → fresh review path.
        return _do_fresh_review(repo, pr, state, dry_run)

    # We have prior state. Reconsider handles all triggers (head changed,
    # author activity, re-request). `force` bypasses the no-activity skip.
    return reconsider_pr(repo, pr, state, dry_run, forced=force)


def _do_fresh_review(repo: str, pr: PullRequest, state: dict[str, Any],
                     dry_run: bool) -> str:
    logging.info("Hydrating PR #%s (%s) by %s", pr.number, pr.title, pr.author)
    hydrate_pr(repo, pr)
    if not pr.diff.strip():
        logging.info("PR #%s has empty diff; skipping", pr.number)
        return "skip-empty"

    prompt = build_review_prompt(pr)
    review = run_copilot_review(prompt)
    artifact = save_review_artifact(pr, review)
    append_metrics(pr, review, kind="review", dry_run=dry_run,
                   artifact=artifact)
    logging.info("Review for PR #%s -> %s (%d comments) saved %s",
                 pr.number, review["decision"], len(review["comments"]),
                 artifact.name)

    review_id: str | None = None
    if dry_run:
        print("\n" + "=" * 78)
        print(f"DRY-RUN PR #{pr.number} by {pr.author}: {pr.title}")
        print(f"  decision: {review['decision']}")
        print(f"  comments: {len(review['comments'])}")
        print(f"  summary : {review['summary'][:300]}")
        print(f"  url     : {pr.url}")
        print("=" * 78)
    else:
        review_id = post_review(repo, pr, review)
        logging.info("Posted %s review on PR #%s (review_id=%s)",
                     review["decision"], pr.number, review_id)

    _record_initial_review(state, pr, review, review_id, dry_run)
    return f"reviewed:{review['decision']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not post to GitHub; just print and save artifacts.")
    ap.add_argument("--force", action="store_true",
                    help="Re-review even if HEAD SHA hasn't changed.")
    ap.add_argument("--only-pr", type=int, default=None,
                    help="Only review this PR number (must still be one I am a requested reviewer on).")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    try:
        me = get_viewer_login()
    except Exception as e:
        logging.error("Could not resolve gh viewer login: %s", e)
        return 2
    logging.info("auto_review starting: repo=%s viewer=%s dry_run=%s",
                 args.repo, me, args.dry_run)

    state = load_state()
    try:
        prs = list_open_prs(args.repo)
    except Exception as e:
        logging.error("Failed to list PRs: %s", e)
        return 2
    logging.info("Found %d open non-draft PR(s) with review requested for %s",
                 len(prs), me)

    if args.only_pr is not None:
        prs = [p for p in prs if p.number == args.only_pr]
        if not prs:
            logging.warning("PR #%s not currently in 'review-requested:@me' set",
                            args.only_pr)

    results: list[tuple[int, str]] = []
    for pr in prs:
        try:
            status = review_pr(args.repo, pr, state, args.dry_run, args.force)
        except subprocess.TimeoutExpired:
            logging.error("Timeout reviewing PR #%s", pr.number)
            status = "error:timeout"
        except Exception as e:
            logging.exception("Failed reviewing PR #%s: %s", pr.number, e)
            status = f"error:{type(e).__name__}"
        results.append((pr.number, status))
        # Persist after every PR so a crash doesn't lose progress.
        save_state(state)

    print("\nSummary:")
    for num, status in results:
        print(f"  PR #{num}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
