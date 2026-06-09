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


def render_html(records: list[dict], hours: int) -> str:
    now_local = datetime.now().strftime("%a %b %d %Y %H:%M %Z").strip()
    if not records:
        return f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;">
<h2>🤖 Agentic-Automations Auto-Review — daily report</h2>
<p>Window: last {hours}h (as of {escape(now_local)}).</p>
<p><em>No reviews in this window.</em></p>
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
        kinds = "+".join(e.get("kind","?") for e in events)
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

    return f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1f2328;max-width:980px;">
<h2 style="margin-bottom:4px;">🤖 Agentic-Automations Auto-Review — daily report</h2>
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
