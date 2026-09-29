"""Tavily search adapter. Maps documented result title/url/content fields."""
from __future__ import annotations

import requests

from .base import SearchHit, SearchProvider
from .search_errors import SearchConfigurationError, SearchRequestError
from .search_http import get_json

SEARCH_URL = "https://api.tavily.com/search"


class TavilySearchProvider(SearchProvider):
    provider_name = "tavily"

    def __init__(self, api_key: str | None):
        if not api_key:
            raise SearchConfigurationError("Tavily search requires TAVILY_API_KEY.")
        self.api_key = api_key

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Tavily search query must be non-empty.")
        data = get_json("Tavily", lambda: requests.post(
            SEARCH_URL, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"query": query.strip(), "max_results": min(max(num_results, 1), 10), "search_depth": "basic"}, timeout=20,
        ))
        rows = data.get("results", [])
        if not isinstance(rows, list):
            raise SearchRequestError("Tavily search returned an unexpected results shape.")
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"].strip():
                continue
            metadata = {"provider": self.provider_name}
            for key in ("score", "published_date"):
                if key in row: metadata[key] = row[key]
            snippet = row.get("content") if isinstance(row.get("content"), str) else ""
            hits.append(SearchHit(title=row.get("title") if isinstance(row.get("title"), str) else "", url=row["url"], snippet=snippet, metadata=metadata))
        return hits
