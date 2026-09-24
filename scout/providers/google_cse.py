from __future__ import annotations

"""Google Custom Search JSON API, wrapped to the SearchProvider interface.
Same backend as before -- just moved behind the interface."""
import requests
from .base import SearchProvider, SearchHit

SEARCH_URL = "https://www.googleapis.com/customsearch/v1"


class GoogleCSEProvider(SearchProvider):
    def __init__(self, api_key: str, cx: str):
        self.api_key = api_key
        self.cx = cx

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        resp = requests.get(
            SEARCH_URL,
            params={"key": self.api_key, "cx": self.cx, "q": query, "num": min(num_results, 10)},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchHit(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            )
            for item in data.get("items", [])
        ]
