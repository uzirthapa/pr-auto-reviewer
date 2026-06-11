"""Per-installation configuration loader.

Reads config.json next to this file (created by setup.py). Missing or
malformed config is OK — scripts that consume it fall back to safe
defaults so a fresh checkout without config still imports cleanly.

Schema (all optional):
  gh_host                 GitHub host, e.g. "microsoft.ghe.com" or "github.com"
  repo                    "owner/name" of the repo to review
  report_recipient        email address for the daily summary
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
