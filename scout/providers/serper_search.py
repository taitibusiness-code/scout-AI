"""Serper search adapter. Maps documented Google-style organic result rows."""
from __future__ import annotations

import requests

from .base import SearchHit, SearchProvider
from .search_errors import SearchConfigurationError, SearchRequestError
from .search_http import get_json

SEARCH_URL = "https://google.serper.dev/search"


class SerperSearchProvider(SearchProvider):
    provider_name = "serper"

    def __init__(self, api_key: str | None):
        if not api_key:
            raise SearchConfigurationError("Serper search requires SERPER_API_KEY.")
        self.api_key = api_key

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Serper search query must be non-empty.")
        data = get_json("Serper", lambda: requests.post(
            SEARCH_URL, headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query.strip(), "num": min(max(num_results, 1), 10)}, timeout=20,
        ))
        rows = data.get("organic", [])
        if not isinstance(rows, list):
            raise SearchRequestError("Serper search returned an unexpected organic-results shape.")
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("link"), str) or not row["link"].strip():
                continue
            metadata = {"provider": self.provider_name}
            if "position" in row: metadata["position"] = row["position"]
            hits.append(SearchHit(title=row.get("title") if isinstance(row.get("title"), str) else "", url=row["link"], snippet=row.get("snippet") if isinstance(row.get("snippet"), str) else "", metadata=metadata))
        return hits
