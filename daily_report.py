#!/usr/bin/env python3
"""Emit an HTML summary of auto-review activity over a recent window.

Reads `reviews/metrics.jsonl` (one JSON record per review/reconsider),
filters to the last N hours (default 24), and prints an HTML body to
stdout. The PowerShell sender pipes that into Outlook.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import config as _user_config

SCRIPT_DIR = Path(__file__).resolve().parent
METRICS = SCRIPT_DIR / "reviews" / "metrics.jsonl"
REVIEWS_DIR = SCRIPT_DIR / "reviews"

VERDICT_BADGE = {
    "approve":         '<span style="color:#1a7f37;font-weight:600;">✅ APPROVE</span>',
    "request_changes": '<span style="color:#cf222e;font-weight:600;">🛑 REQUEST CHANGES</span>',
    "comment":         '<span style="color:#9a6700;font-weight:600;">💬 COMMENT</span>',
}


def load_summary_for(rec: dict) -> str:
    """Load the human-readable summary text the model wrote for a review.

    The metrics record only stores counts; the full review (decision +
    summary + inline comments) is in `reviews/<artifact_path>`. Returns
    an empty string if the artifact is missing or malformed.
    """
    art = rec.get("artifact_path")
    if not art:
        return ""
    p = REVIEWS_DIR / art
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    review = data.get("review") or {}
    return (review.get("summary") or "").strip()


def load_records(hours: int) -> list[dict]:
    if not METRICS.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    with METRICS.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            at = rec.get("at_iso")
            if not at:
                continue
            try:
                # Trailing Z → UTC
                ts = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= cutoff:
                rec["_ts"] = ts
                out.append(rec)
    return out


def _format_kinds(events: list[dict]) -> str:
    """Compact, email-friendly summary of a PR's event types.

    The old `"+".join(...)` produced an ever-growing unbreakable token like
    `review+reconsider+reconsider+reconsider+...` that stretched the column
    horizontally. Collapse repeats into counts and use spaces so the cell
    can wrap: e.g. "review", "5 re-reviews", "review + 5 re-reviews".
    """
    n_review = sum(1 for e in events if e.get("kind") == "review")
    n_recon = sum(1 for e in events if e.get("kind") == "reconsider")
    parts = []
    if n_review:
        parts.append("review" if n_review == 1 else f"{n_review} reviews")
    if n_recon:
        parts.append("re-review" if n_recon == 1 else f"{n_recon} re-reviews")
    return " + ".join(parts) or "?"


def _migration_banner_html() -> str:
    """Prominent action-required banner shown when `review_authors` is not
    configured. Existing installs that predate the author-based model run
    the 5-min task silently and now review NOTHING until an author list is
    set — most users never see the log warning, so we surface it here in
    the one channel that lands in their inbox.

    Returns an empty string once `review_authors` is configured.
    """
    env = os.environ.get("COPILOT_REVIEW_AUTHORS")
    configured = [a.strip() for a in env.split(",")] if env is not None \
        else (_user_config.get("review_authors") or [])
    if any(str(a).strip() for a in configured):
        return ""
    return """
<div style="border:2px solid #cf222e;border-radius:6px;background:#ffebe9;
            padding:12px 16px;margin:0 0 18px 0;color:#1f2328;">
  <div style="font-weight:700;color:#cf222e;font-size:15px;margin-bottom:4px;">
    ⚠️ Action required — no one's PRs are being reviewed
  </div>
  <div style="font-size:14px;line-height:1.45;">
    This auto-reviewer now reviews PRs by a configured list of authors
    instead of whoever you're requested to review. You haven't set that
    list yet, so <strong>nothing is being reviewed</strong>.
    Set whose PRs to review by re-running
    <code>python setup.py</code> (or add a <code>review_authors</code>
    array to <code>config.json</code> / set the
    <code>COPILOT_REVIEW_AUTHORS</code> env var).
  </div>
</div>"""


def _learned_blurb_html(learn_records: list[dict]) -> str:
    """Small 'what I learned' blurb for the daily email, built from
    `kind:"self_improve"` metrics records in the window. Returns "" when the
    learn step didn't run at all in the window."""
    if not learn_records:
        return ""
    items: list[dict] = []
    considered = 0
    prs: set = set()
    for r in learn_records:
        considered += r.get("comments_considered") or 0
        for n in (r.get("source_prs") or []):
            prs.add(n)
        for it in (r.get("learned") or []):
            if (it.get("text") or "").strip():
                items.append(it)

    if not items:
        # The step ran but found nothing new worth adding — say so briefly.
        return f"""
<h3 style="margin:22px 0 6px 0;">🧠 What I learned</h3>
<p style="color:#57606a;margin-top:0;font-size:13px;">
  Studied {considered} comment(s) from other reviewers across
  {len(prs)} PR(s) — nothing new to add to my review guidance this time.
</p>"""

    # Group learned bullets by category.
    by_cat: dict[str, list[dict]] = {}
    for it in items:
        by_cat.setdefault(it.get("category") or "General", []).append(it)
    cat_html: list[str] = []
    for cat, group in by_cat.items():
        lis = "".join(
            f'<li style="margin:2px 0;">'
            f'{"🚫 " if (it.get("kind") == "avoid") else ""}{escape(it.get("text") or "")}'
            f'</li>'
            for it in group
        )
        cat_html.append(
            f'<div style="margin:6px 0;"><strong>{escape(cat)}</strong>'
            f'<ul style="margin:4px 0 4px 18px;padding:0;">{lis}</ul></div>'
        )

    return f"""
<h3 style="margin:22px 0 6px 0;">🧠 What I learned</h3>
<p style="color:#57606a;margin-top:0;font-size:13px;">
  Studied {considered} comment(s) from other reviewers across {len(prs)}
  PR(s) and folded <strong>{len(items)}</strong> new lesson(s) into my
  review guidance (also saved to the <code>memory/</code> wiki).</p>
<div style="border:1px solid #d0d7de;border-radius:6px;padding:10px 16px;
            margin:8px 0;background:#f6f8fa;font-size:14px;line-height:1.4;">
  {''.join(cat_html)}
</div>"""


def _needs_human_review_html(by_pr: dict[int, dict]) -> str:
    """Prominent call-out listing PRs whose latest verdict flagged a core
    functionality change. These are blocked and require a human to review
    and (with auto-approval off) approve them manually. Returns "" when
    none are flagged."""
    flagged = [
        slot["latest"] for slot in by_pr.values()
        if slot["latest"].get("needs_human_review")
    ]
    if not flagged:
        return ""
    flagged.sort(key=lambda r: r["_ts"], reverse=True)
    items = []
    for r in flagged:
        n = r.get("pr_number")
        url = escape(r.get("pr_url") or "#")
        author = escape(r.get("pr_author") or "?")
        title = escape(r.get("pr_title") or "")
        pct = r.get("core_functionality_change_pct")
        pct_txt = f" · ~{pct}% of core changed" if pct else ""
        items.append(
            f'<li style="margin:3px 0;">'
            f'<a href="{url}" style="text-decoration:none;font-weight:600;">#{n}</a>'
            f' <span style="color:#57606a;">· {author}'
            f'{(" · " + title) if title else ""}{pct_txt}</span></li>'
        )
    return f"""
<div style="border:2px solid #9a6700;border-radius:6px;background:#fff8c5;
            padding:12px 16px;margin:0 0 18px 0;color:#1f2328;">
  <div style="font-weight:700;color:#9a6700;font-size:15px;margin-bottom:4px;">
    🚩 {len(flagged)} PR(s) need human review — high-impact core functionality change
  </div>
  <div style="font-size:14px;line-height:1.45;">
    These PRs change a large share of core / main-page functionality and were
    <strong>blocked</strong> by the automated reviewer. A human must review
    (and approve) them before merge.
    <ul style="margin:6px 0 0 18px;padding:0;">{''.join(items)}</ul>
  </div>
</div>"""


def render_html(records: list[dict], hours: int) -> str:
    now_local = datetime.now().strftime("%a %b %d %Y %H:%M %Z").strip()
    banner = _migration_banner_html()
    # Self-improvement records are surfaced in their own blurb, not the
    # per-PR review table/counters.
    learn_records = [r for r in records if r.get("kind") == "self_improve"]
    records = [r for r in records if r.get("kind") in ("review", "reconsider")]
    learned_html = _learned_blurb_html(learn_records)
    if not records:
        return f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;">
<h2>🤖 Agentic-Automations Auto-Review — daily report</h2>
{banner}
<p>Window: last {hours}h (as of {escape(now_local)}).</p>
<p><em>No reviews in this window.</em></p>
{learned_html}
</body></html>"""

    reviews     = [r for r in records if r.get("kind") == "review"]
    reconsiders = [r for r in records if r.get("kind") == "reconsider"]

    decision_counts = Counter(r.get("decision") for r in records)
    total_issues = sum(r.get("issues_count") or 0 for r in records)
    blocks_issued = sum(1 for r in records if r.get("decision") == "request_changes")
    blocks_lifted = sum(1 for r in reconsiders if r.get("block_lifted"))
    files_touched = sorted({f for r in records for f in (r.get("files_flagged") or [])})

    # Per-PR latest action (one row per PR, with both initial + reconsider info).
    by_pr: dict[int, dict] = {}
    for r in sorted(records, key=lambda r: r["_ts"]):
        n = r.get("pr_number")
        if n is None:
            continue
        slot = by_pr.setdefault(n, {"events": []})
        slot["events"].append(r)
        slot["latest"] = r
    rows_html = []
    for n in sorted(by_pr.keys(), key=lambda n: by_pr[n]["latest"]["_ts"], reverse=True):
        slot = by_pr[n]
        latest = slot["latest"]
        events = slot["events"]
        kinds = _format_kinds(events)
        issues_total = sum(e.get("issues_count") or 0 for e in events)
        url = latest.get("pr_url") or "#"
        author = escape(latest.get("pr_author") or "?")
        verdict = VERDICT_BADGE.get(latest.get("decision",""), escape(latest.get("decision","?")))
        ts = latest["_ts"].astimezone().strftime("%H:%M")
        notes = []
        if latest.get("kind") == "reconsider":
            if latest.get("block_lifted"):
                notes.append("block lifted")
            apb = latest.get("addresses_prior_block")
            if apb is False:
                notes.append("prior block still stands")
            rem = latest.get("remaining_concerns_count") or 0
            if rem:
                notes.append(f"{rem} remaining concern(s)")
            t = latest.get("trigger") or {}
            trig_parts = []
            if t.get("new_commits"): trig_parts.append(f"{t['new_commits']} commit(s)")
            if t.get("rerequests"):  trig_parts.append(f"{t['rerequests']} re-request")
            if (t.get("issue_comments") or 0) + (t.get("review_comments") or 0):
                trig_parts.append("author replies")
            if trig_parts:
                notes.append("triggered by " + ", ".join(trig_parts))
        if latest.get("needs_human_review"):
            _pct = latest.get("core_functionality_change_pct")
            _pct_txt = f" (~{_pct}% of core)" if _pct else ""
            notes.append(f"🚩 core functionality change{_pct_txt} — needs human review")
        if latest.get("manual_approval_pending"):
            notes.append("not yet approved on GitHub — awaiting manual approval")
        if latest.get("dry_run"):
            notes.append("dry-run")
        notes_html = ("<br/><span style='color:#57606a;font-size:12px;'>"
                      + escape("; ".join(notes)) + "</span>") if notes else ""
        rows_html.append(f"""
<tr>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;">
    <a href="{escape(url)}" style="text-decoration:none;">#{n}</a>
  </td>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;">{author}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;">{verdict}{notes_html}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;text-align:right;">{issues_total}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;color:#57606a;">{escape(kinds)}</td>
  <td style="padding:6px 10px;border-bottom:1px solid #d0d7de;color:#57606a;">{escape(ts)}</td>
</tr>""")

    counts_chip = (
        f'<span style="background:#dafbe1;color:#1a7f37;padding:2px 8px;border-radius:12px;">'
        f'{decision_counts.get("approve", 0)} approved</span> &nbsp;'
        f'<span style="background:#ffebe9;color:#cf222e;padding:2px 8px;border-radius:12px;">'
        f'{decision_counts.get("request_changes", 0)} requested changes</span> &nbsp;'
        f'<span style="background:#fff8c5;color:#9a6700;padding:2px 8px;border-radius:12px;">'
        f'{decision_counts.get("comment", 0)} commented</span>'
    )

    # Reasoning detail: show why we blocked each PR. Only request_changes
    # is included — approves and comments are summarized in the table.
    reason_blocks: list[str] = []
    for n in sorted(by_pr.keys(), key=lambda n: by_pr[n]["latest"]["_ts"], reverse=True):
        slot = by_pr[n]
        latest = slot["latest"]
        decision = latest.get("decision")
        if decision != "request_changes":
            continue
        summary = load_summary_for(latest)
        if not summary:
            continue
        url = latest.get("pr_url") or "#"
        title = escape(latest.get("pr_title") or "")
        author = escape(latest.get("pr_author") or "?")
        verdict = VERDICT_BADGE.get(decision, escape(decision or "?"))
        issues = latest.get("issues_count") or 0
        # Render summary as paragraphs, preserving the model's line breaks.
        paragraphs = [escape(p).replace("\n", "<br/>") for p in summary.split("\n\n") if p.strip()]
        body_html = "".join(f'<p style="margin:6px 0;">{p}</p>' for p in paragraphs)
        reason_blocks.append(f"""
<div style="border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;margin:10px 0;background:#fafbfc;">
  <div style="margin-bottom:6px;">
    {verdict} &nbsp;
    <a href="{escape(url)}" style="font-weight:600;text-decoration:none;color:#0969da;">#{n}</a>
    <span style="color:#57606a;"> · {author}{(' · ' + str(issues) + ' issue(s)') if issues else ''}</span>
    {(' · <span style="color:#57606a;">' + title + '</span>') if title else ''}
  </div>
  <div style="color:#1f2328;font-size:14px;line-height:1.45;">{body_html}</div>
</div>""")
    reasoning_html = ""
    if reason_blocks:
        reasoning_html = (
            '<h3 style="margin:22px 0 6px 0;">Why each PR was blocked (request changes)</h3>'
            '<p style="color:#57606a;margin-top:0;font-size:13px;">'
            'Summary the model wrote for every PR that got REQUEST CHANGES. '
            'Approves and comment-only reviews are omitted; see the table above.</p>'
            + "".join(reason_blocks)
        )

    needs_human_html = _needs_human_review_html(by_pr)

    return f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1f2328;max-width:980px;">
<h2 style="margin-bottom:4px;">🤖 Agentic-Automations Auto-Review — daily report</h2>
{banner}
{needs_human_html}
<p style="color:#57606a;margin-top:0;">Window: last {hours}h (as of {escape(now_local)}).</p>

<table style="border-collapse:collapse;margin:12px 0 18px 0;">
  <tr>
    <td style="padding:6px 14px 6px 0;color:#57606a;">Reviews submitted</td>
    <td style="padding:6px 0;"><strong>{len(records)}</strong>
      <span style="color:#57606a;">({len(reviews)} initial · {len(reconsiders)} re-review)</span></td>
  </tr>
  <tr>
    <td style="padding:6px 14px 6px 0;color:#57606a;">Distinct PRs touched</td>
    <td style="padding:6px 0;"><strong>{len(by_pr)}</strong></td>
  </tr>
  <tr>
    <td style="padding:6px 14px 6px 0;color:#57606a;">Decisions</td>
    <td style="padding:6px 0;">{counts_chip}</td>
  </tr>
  <tr>
    <td style="padding:6px 14px 6px 0;color:#57606a;">Issues raised</td>
    <td style="padding:6px 0;"><strong>{total_issues}</strong> across {len(files_touched)} file(s)</td>
  </tr>
  <tr>
    <td style="padding:6px 14px 6px 0;color:#57606a;">Blocks issued / lifted</td>
    <td style="padding:6px 0;"><strong>{blocks_issued}</strong> issued · <strong>{blocks_lifted}</strong> lifted on re-review</td>
  </tr>
</table>

<h3 style="margin-bottom:6px;">Per-PR activity (latest action, most recent first)</h3>
<table style="border-collapse:collapse;width:100%;font-size:14px;">
  <thead>
    <tr style="background:#f6f8fa;text-align:left;">
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;">PR</th>
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;">Author</th>
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;">Latest verdict</th>
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;text-align:right;">Issues</th>
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;">Kinds</th>
      <th style="padding:6px 10px;border-bottom:1px solid #d0d7de;">Time</th>
    </tr>
  </thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>

{reasoning_html}

{learned_html}

<p style="color:#57606a;font-size:12px;margin-top:18px;">
  Sourced from <code>reviews\\metrics.jsonl</code>. Full text of each review lives in <code>reviews\\pr-&lt;num&gt;-&lt;sha&gt;.json</code> next to it.
</p>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=int(os.environ.get("REPORT_HOURS", "24")),
                    help="Lookback window in hours (default 24).")
    ap.add_argument("--output", type=str, default=None,
                    help="Write HTML to this path instead of stdout.")
    args = ap.parse_args()

    html = render_html(load_records(args.hours), args.hours)
    if args.output:
        Path(args.output).write_text(html, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
