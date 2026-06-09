#!/usr/bin/env python3
"""One-shot: re-review every PR in state.json whose prior decision was
"comment" under the new binary (approve / request_changes) rubric.

Calls `auto_review.py --only-pr <n> --force` for each. `--force` makes
reconsider_pr bypass the "no new activity" skip.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
AUTO = ROOT / "auto_review.py"


def main() -> int:
    if not STATE.exists():
        print("state.json not found", file=sys.stderr)
        return 2
    data = json.loads(STATE.read_text(encoding="utf-8"))
    prs_map = data.get("prs") if isinstance(data.get("prs"), dict) else data
    targets = sorted(
        int(k) for k, v in prs_map.items()
        if isinstance(v, dict) and v.get("decision") == "comment"
    )
    if not targets:
        print("No PRs with prior decision='comment' — nothing to do.")
        return 0
    print(f"Re-reviewing {len(targets)} PR(s) under the new binary rubric: {targets}")
    results: list[tuple[int, int, str]] = []
    start = time.time()
    for i, n in enumerate(targets, 1):
        elapsed = int(time.time() - start)
        print(f"\n[{i}/{len(targets)}] PR #{n}  (elapsed: {elapsed//60}m {elapsed%60}s)")
        cmd = [sys.executable, str(AUTO), "--only-pr", str(n), "--force"]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        except subprocess.TimeoutExpired:
            print(f"  PR #{n}: TIMEOUT after 1200s")
            results.append((n, -1, "timeout"))
            continue
        last_lines = "\n".join((cp.stdout or "").splitlines()[-6:])
        print(last_lines)
        if cp.returncode != 0:
            err_tail = "\n".join((cp.stderr or "").splitlines()[-4:])
            print(f"  PR #{n}: rc={cp.returncode}\n{err_tail}")
        results.append((n, cp.returncode, last_lines))
    total = int(time.time() - start)
    print(f"\n==== Done in {total//60}m {total%60}s ====")
    for n, rc, _ in results:
        print(f"  PR #{n}: rc={rc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
