from __future__ import annotations

"""Config: all keys/paths come from environment variables. Nothing hardcoded.

Environment variables (validated by the command that needs them):
  SCOUT_LLM_PROVIDER        - ollama (default) or anthropic
  ANTHROPIC_API_KEY         - only when the Anthropic LLM is selected
  SCOUT_OLLAMA_MODEL        - default: qwen3:8b
  SCOUT_OLLAMA_BASE_URL     - default: http://127.0.0.1:11434
  GOOGLE_CSE_API_KEY        - Google Custom Search JSON API key for discovery
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
    llm_provider: str
    anthropic_api_key: str | None
    google_cse_api_key: str | None
    google_cse_cx: str | None
    db_path: str
    log_path: str
    anthropic_model: str = "claude-sonnet-4-6"
    ollama_model: str = "qwen3:8b"
    ollama_base_url: str = "http://127.0.0.1:11434"


LLM_PROVIDERS = ("ollama", "anthropic")


def load_config(require_llm: bool = False) -> Config:
    provider = os.environ.get("SCOUT_LLM_PROVIDER", "ollama").strip().lower()
    if provider not in LLM_PROVIDERS:
        raise RuntimeError(f"SCOUT_LLM_PROVIDER must be one of: {', '.join(LLM_PROVIDERS)}")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if require_llm and provider == "anthropic" and not anthropic_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. It is required when SCOUT_LLM_PROVIDER=anthropic."
        )
    return Config(
        llm_provider=provider,
        anthropic_api_key=anthropic_key,
        google_cse_api_key=os.environ.get("GOOGLE_CSE_API_KEY"),
        google_cse_cx=os.environ.get("GOOGLE_CSE_CX"),
        db_path=os.environ.get("SCOUT_DB_PATH", "./scout.db"),
        log_path=os.environ.get("SCOUT_LOG_PATH", "./scout_log.jsonl"),
        anthropic_model=os.environ.get("SCOUT_ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        ollama_model=os.environ.get("SCOUT_OLLAMA_MODEL", "qwen3:8b"),
        ollama_base_url=os.environ.get("SCOUT_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
    )
