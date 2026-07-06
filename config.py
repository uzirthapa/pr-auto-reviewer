"""Per-installation configuration loader.

Reads config.json next to this file (created by setup.py). Missing or
malformed config is OK — scripts that consume it fall back to safe
defaults so a fresh checkout without config still imports cleanly.

Schema (all optional):
  gh_host                 GitHub host, e.g. "microsoft.ghe.com" or "github.com"
  repo                    "owner/name" of the repo to review
  review_authors          list[str] of GitHub logins whose open PRs are
                          reviewed. Replaces the old review-requested:@me
                          model. Overridden by the COPILOT_REVIEW_AUTHORS
                          env var (comma-separated). Empty/missing means
                          nothing is reviewed.
  report_recipient        email address for the daily summary
  report_time             "HH:MM" 24h local time to send the daily report
  review_model            Copilot model id for reviews (e.g.
                          "claude-opus-4.8"). Omit to auto-use the latest
                          Opus the local Copilot CLI is set to. Overridden
                          by the COPILOT_REVIEW_MODEL env var.
  ai_provider             which AI CLI runs the review. One of the built-in
                          presets: "copilot" (default), "agency" (Microsoft
                          Agency wrapper around Copilot), or "claude"
                          (Anthropic Claude CLI). Overridden by the
                          COPILOT_REVIEW_AI_PROVIDER env var.
  ai_command              advanced: base command tokens for a fully custom
                          AI CLI (string or list), e.g. ["mytool","run"].
                          Overrides ai_provider. Env: COPILOT_REVIEW_AI_COMMAND.
  ai_args                 advanced: argument template (list) for the custom
                          CLI. Placeholders __MODEL__, __EFFORT__,
                          __CONTEXT__, __DIR__, __PROMPT__ are substituted
                          at call time. The CLI must write its JSON answer
                          to review_output.json in __DIR__, or print it to
                          stdout.
  review_concurrency      how many PRs to review in parallel (int, 1-10,
                          default 5). Overridden by the
                          COPILOT_REVIEW_CONCURRENCY env var.
  priority_authors        list[str] of GitHub logins whose PRs are
                          reviewed first each cycle. Overridden by the
                          COPILOT_REVIEW_PRIORITY_AUTHORS env var
                          (comma-separated).
  codebase_description    one-sentence description of the codebase,
                          inlined into the reviewer prompt so the model
                          has context about the product/stack
  review_focus            list[str] of extra things the reviewer should
                          look out for (appended to REVIEW_INSTRUCTIONS)
  review_avoid            list[str] of extra things the reviewer should
                          NOT comment on
  reviewer_style          free-form prose describing the desired
                          reviewer tone / depth (appended verbatim)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _load() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


CONFIG: dict[str, Any] = _load()


def get(key: str, default: Any = None) -> Any:
    return CONFIG.get(key, default)


def path() -> Path:
    return _CONFIG_PATH


def has_config() -> bool:
    return _CONFIG_PATH.exists()
