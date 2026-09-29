"""Abstract base classes for the three provider interfaces.

Design rule: these interfaces return plain dataclasses, never provider-native
objects (no raw Anthropic response objects, no raw requests.Response). That's
what makes swapping providers safe -- the rest of Scout only ever sees these
shapes.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    title: Optional[str]
    text_content: str
    html_meta: dict
    error: Optional[str] = None


@dataclass
class LLMToolResult:
    """Result of a forced tool-call LLM extraction -- provider-agnostic."""
    tool_name: str
    input: dict            # the structured JSON the model produced
    raw_text: str = ""     # any accompanying prose, if the provider gives one


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        ...


class BrowserProvider(ABC):
    @abstractmethod
    def fetch(self, url: str, timeout: int = 15, max_chars: int = 8000) -> FetchResult:
        ...


class LLMProvider(ABC):
    @abstractmethod
    def extract(self, system: str, user_content: str, tool_name: str,
                tool_description: str, input_schema: dict,
                max_tokens: int = 1000) -> LLMToolResult:
        """Force a structured tool-call response matching input_schema.
        Every module that needs structured output from an LLM goes through
        this one method -- it's the only thing a new LLMProvider must
        implement correctly."""
        ...
