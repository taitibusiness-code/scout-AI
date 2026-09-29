"""Safe, provider-neutral search errors used by adapters and fallback logic."""
from __future__ import annotations


class SearchProviderError(RuntimeError):
    """A safe error suitable for CLI output; it must never contain a key."""


class SearchConfigurationError(SearchProviderError):
    """Missing credentials or an unsupported provider selection."""


class SearchRequestError(SearchProviderError):
    """A bad query or non-retryable provider request/response."""


class SearchAuthenticationError(SearchRequestError):
    """Credentials were rejected; do not consume another provider's quota."""


class TransientSearchError(SearchProviderError):
    """A timeout, connection, quota, or server condition that may recover."""


class AllSearchProvidersFailed(TransientSearchError):
    """Every explicitly configured fallback provider failed transiently."""
