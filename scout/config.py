from __future__ import annotations

"""Config: all keys/paths come from environment variables. Nothing hardcoded.

Required:
  ANTHROPIC_API_KEY        - for the understand/analyze LLM steps
  GOOGLE_CSE_API_KEY        - Google Custom Search JSON API key (discover step)
  GOOGLE_CSE_CX             - Custom Search Engine ID (create one at
                              https://programmablesearchengine.google.com/,
                              set it to search the whole web)

Optional:
  SCOUT_DB_PATH             - default: ./scout.db
  SCOUT_LOG_PATH             - default: ./scout_log.jsonl
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str | None
    google_cse_api_key: str | None
    google_cse_cx: str | None
    db_path: str
    log_path: str
    model: str = "claude-sonnet-4-6"


def load_config(require_llm: bool = True) -> Config:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if require_llm and not anthropic_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Scout's understand/analyze steps "
            "call the Anthropic API directly and cannot run without it."
        )
    return Config(
        anthropic_api_key=anthropic_key,
        google_cse_api_key=os.environ.get("GOOGLE_CSE_API_KEY"),
        google_cse_cx=os.environ.get("GOOGLE_CSE_CX"),
        db_path=os.environ.get("SCOUT_DB_PATH", "./scout.db"),
        log_path=os.environ.get("SCOUT_LOG_PATH", "./scout_log.jsonl"),
    )
