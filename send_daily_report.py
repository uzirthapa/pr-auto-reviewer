#!/usr/bin/env python3
"""Generate the daily auto-review report and email it via Outlook (COM).

We use Outlook COM via PowerShell because it piggy-backs on the user's
already-signed-in Outlook profile — no extra auth, no SMTP creds, no
MCP dependency. Works unattended as long as the user session exists.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from daily_report import render_html, load_records
import config as _user_config

# Suppress Windows console flash when launched under pythonw.exe.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "daily_report.log"

DEFAULT_RECIPIENT = os.environ.get(
    "REPORT_RECIPIENT",
    _user_config.get("report_recipient", ""),
)
SEND_TIMEOUT = int(os.environ.get("REPORT_TIMEOUT", "180"))


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(message)s"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=level, format=fmt,
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


_PS_SEND_SCRIPT = r'''
param(
    [Parameter(Mandatory=$true)] [string]$To,
    [Parameter(Mandatory=$true)] [string]$Subject,
    [Parameter(Mandatory=$true)] [string]$HtmlPath
)
$ErrorActionPreference = "Stop"
$html = Get-Content -LiteralPath $HtmlPath -Raw -Encoding UTF8

# Outlook COM Send() only QUEUES the mail to the Outbox; it does not
# transmit. If outlook.exe isn't already running with its send/receive
# scheduler (e.g. the scheduled task fires right after the machine wakes,
# before the user has opened Outlook), the mail sits in the Outbox and is
# never delivered -- yet Send() returns success. So: make sure Outlook is
# actually running, send, force a send/receive, and verify the Outbox
# drains before reporting SENT.
if (-not (Get-Process -Name OUTLOOK -ErrorAction SilentlyContinue)) {
    Start-Process "outlook.exe" | Out-Null
    Start-Sleep -Seconds 30   # let the profile load and connect
}

$outlook = New-Object -ComObject Outlook.Application
$ns      = $outlook.GetNamespace("MAPI")
$mail    = $outlook.CreateItem(0)   # 0 = MailItem
$mail.To       = $To
$mail.Subject  = $Subject
$mail.BodyFormat = 2                # 2 = olFormatHTML
$mail.HTMLBody = $html
$mail.Send()

# Force transmission and wait for the Outbox to actually drain so we never
# report a false success on mail that's merely queued.
$outbox = $ns.GetDefaultFolder(4)   # 4 = olFolderOutbox
$deadline = (Get-Date).AddSeconds(60)
do {
    try { $ns.SendAndReceive($false) } catch {}
    Start-Sleep -Seconds 4
} while ($outbox.Items.Count -gt 0 -and (Get-Date) -lt $deadline)

if ($outbox.Items.Count -gt 0) {
    throw "Mail still in Outbox after send/receive -- not delivered (Outlook offline?)."
}
Write-Output "SENT"
'''


def send_via_outlook(html: str, recipient: str, subject: str) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="daily_report_"))
    html_file = tmp_dir / "report.html"
    ps_file   = tmp_dir / "send.ps1"
    html_file.write_text(html, encoding="utf-8")
    ps_file.write_text(_PS_SEND_SCRIPT, encoding="utf-8")

    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(ps_file),
        "-To", recipient,
        "-Subject", subject,
        "-HtmlPath", str(html_file),
    ]
    logging.info("Sending via Outlook COM (recipient=%s, html=%d bytes)",
                 recipient, len(html))
    start = time.time()
    res = subprocess.run(
        cmd, capture_output=True, timeout=SEND_TIMEOUT,
        encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    dur = time.time() - start
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    logging.info("PowerShell returned in %.1fs rc=%d", dur, res.returncode)
    if res.returncode != 0 or "SENT" not in out:
        logging.error("Outlook send failed.\nSTDOUT: %s\nSTDERR: %s",
                      out[:1500], err[:1500])
        raise RuntimeError(f"outlook send failed (rc={res.returncode})")
    logging.info("Report sent to %s", recipient)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=int(os.environ.get("REPORT_HOURS", "24")),
                    help="Lookback window in hours (default 24).")
    ap.add_argument("--recipient", default=DEFAULT_RECIPIENT)
    ap.add_argument("--dry-run", action="store_true",
                    help="Render the report and write it to a local file; do not email.")
    ap.add_argument("--output", default=None,
                    help="When --dry-run, write HTML here (default: ./daily_report.html).")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--include-weekends", action="store_true",
                    help="Send even on Saturday/Sunday (default: skip weekends "
                         "for live sends; --dry-run always runs).")
    args = ap.parse_args()

    setup_logging(args.verbose)

    records = load_records(args.hours)
    html = render_html(records, args.hours)
    logging.info("Rendered report: %d records in window, %d bytes of HTML",
                 len(records), len(html))

    if args.dry_run:
        out_path = Path(args.output) if args.output else SCRIPT_DIR / "daily_report.html"
        out_path.write_text(html, encoding="utf-8")
        logging.info("DRY-RUN: report written to %s (open in a browser to preview)", out_path)
        return 0

    # weekday() -> Mon=0 ... Sun=6. Skip Sat (5) and Sun (6) unless overridden.
    today = datetime.now()
    if today.weekday() >= 5 and not args.include_weekends:
        logging.info("Skipping send: today is %s (use --include-weekends to override).",
                     today.strftime("%A"))
        return 0

    subject = f"Auto-review report — {today.strftime('%a %b %d %Y')}"
    send_via_outlook(html, args.recipient, subject)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.exception("daily report failed")
        sys.exit(1)

