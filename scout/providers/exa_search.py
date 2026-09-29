"""Exa search adapter. Uses the documented /search results/highlights shape."""
from __future__ import annotations

import requests

from .base import SearchHit, SearchProvider
from .search_errors import SearchConfigurationError, SearchRequestError
from .search_http import get_json

SEARCH_URL = "https://api.exa.ai/search"


class ExaSearchProvider(SearchProvider):
    provider_name = "exa"

    def __init__(self, api_key: str | None):
        if not api_key:
            raise SearchConfigurationError("Exa search requires EXA_API_KEY.")
        self.api_key = api_key

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Exa search query must be non-empty.")
        data = get_json("Exa", lambda: requests.post(
            SEARCH_URL, headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json={"query": query.strip(), "numResults": min(max(num_results, 1), 10),
                  "contents": {"highlights": {"maxCharacters": 500}}}, timeout=20,
        ))
        rows = data.get("results", [])
        if not isinstance(rows, list):
            raise SearchRequestError("Exa search returned an unexpected results shape.")
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"].strip():
                continue
            highlights = row.get("highlights", [])
            snippet = " ".join(value for value in highlights if isinstance(value, str)) if isinstance(highlights, list) else ""
            snippet = snippet or (row.get("text") if isinstance(row.get("text"), str) else "")
            metadata = {"provider": self.provider_name}
            for key in ("id", "score", "publishedDate", "author"):
                if key in row: metadata[key] = row[key]
            hits.append(SearchHit(title=row.get("title") if isinstance(row.get("title"), str) else "", url=row["url"], snippet=snippet, metadata=metadata))
        return hits
