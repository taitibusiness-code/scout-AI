"""Provider interfaces: Search / Browser / LLM.

Nothing else in Scout should import `anthropic` or `requests` directly again
after this. Every module that needs to search, fetch a page, or call an LLM
takes a provider instance and calls its interface methods. Swapping Google
CSE for another search API, or Anthropic for local Ollama, means writing one
new class here -- zero changes anywhere else in the pipeline.
"""
from .base import SearchProvider, BrowserProvider, LLMProvider, SearchHit, LLMToolResult

__all__ = ["SearchProvider", "BrowserProvider", "LLMProvider", "SearchHit", "LLMToolResult"]
