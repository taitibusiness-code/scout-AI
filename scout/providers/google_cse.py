from __future__ import annotations

"""Google Custom Search JSON API, wrapped to the SearchProvider interface.
Same backend as before -- just moved behind the interface."""
import requests
from .base import SearchProvider, SearchHit
from .search_errors import SearchConfigurationError, SearchRequestError
from .search_http import get_json

SEARCH_URL = "https://www.googleapis.com/customsearch/v1"


class GoogleCSEProvider(SearchProvider):
    provider_name = "google_cse"

    def __init__(self, api_key: str | None, cx: str | None):
        if not api_key or not cx:
            raise SearchConfigurationError("Google CSE search requires GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX.")
        self.api_key = api_key
        self.cx = cx

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Google CSE search query must be non-empty.")
        data = get_json("Google CSE", lambda: requests.get(
            SEARCH_URL, params={"key": self.api_key, "cx": self.cx, "q": query.strip(), "num": min(max(num_results, 1), 10)}, timeout=20,
        ))
        rows = data.get("items", [])
        if not isinstance(rows, list):
            raise SearchRequestError("Google CSE search returned an unexpected results shape.")
        return [SearchHit(title=row.get("title") if isinstance(row.get("title"), str) else "",
                          url=row["link"], snippet=row.get("snippet") if isinstance(row.get("snippet"), str) else "",
                          metadata={"provider": self.provider_name})
                for row in rows if isinstance(row, dict) and isinstance(row.get("link"), str) and row["link"].strip()]
