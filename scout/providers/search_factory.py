"""Search-provider selection and deliberately narrow fallback behaviour."""
from __future__ import annotations

from .base import SearchProvider
from .exa_search import ExaSearchProvider
from .google_cse import GoogleCSEProvider
from .search_errors import (AllSearchProvidersFailed, SearchConfigurationError,
                            SearchRequestError, TransientSearchError)
from .serper_search import SerperSearchProvider
from .tavily_search import TavilySearchProvider

SEARCH_PROVIDER_NAMES = ("exa", "tavily", "serper", "google_cse")


class FallbackSearchProvider(SearchProvider):
    """Try only explicitly configured providers, and only after transient errors."""
    def __init__(self, providers: list[SearchProvider]):
        if not providers:
            raise SearchConfigurationError("At least one search provider is required.")
        self.providers = providers
        self.provider_name = "fallback"

    def search(self, query: str, num_results: int = 10):
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Search query must be non-empty.")
        failures = []
        for provider in self.providers:
            try:
                return provider.search(query, num_results)
            except TransientSearchError as error:
                name = getattr(provider, "provider_name", type(provider).__name__)
                failures.append(f"{name}: {error}")
        raise AllSearchProvidersFailed("All configured search providers failed transiently: " + "; ".join(failures))


def _provider_for(name: str, cfg) -> SearchProvider:
    if name == "exa": return ExaSearchProvider(cfg.exa_api_key)
    if name == "tavily": return TavilySearchProvider(cfg.tavily_api_key)
    if name == "serper": return SerperSearchProvider(cfg.serper_api_key)
    if name == "google_cse": return GoogleCSEProvider(cfg.google_cse_api_key, cfg.google_cse_cx)
    raise SearchConfigurationError(f"Unsupported search provider '{name}'. Expected one of: {', '.join(SEARCH_PROVIDER_NAMES)}")


def build_search_provider(cfg) -> SearchProvider:
    """Build one explicit provider or an ordered, explicit fallback chain."""
    names = cfg.search_providers or (cfg.search_provider,)
    providers = [_provider_for(name, cfg) for name in names]
    return providers[0] if len(providers) == 1 else FallbackSearchProvider(providers)
