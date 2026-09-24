from __future__ import annotations

"""DISCOVER step. Goes through a SearchProvider -- never imports a search
library directly. See providers/google_cse.py for the default backend.
"""
from .models import Candidate
from .providers.base import SearchProvider


def discover_search(query: str, search: SearchProvider, num_results: int = 10) -> list[Candidate]:
    hits = search.search(query, num_results=num_results)
    return [
        Candidate(name=hit.title.split(" - ")[0].strip(), source="search",
                  source_url=hit.url, query=query)
        for hit in hits
    ]


def discover_manual(names_and_urls: list[tuple[str, str]], query: str = "manual") -> list[Candidate]:
    return [
        Candidate(name=name, source="manual", source_url=url, query=query)
        for name, url in names_and_urls
    ]
