"""Safe, provider-neutral search errors used by adapters and fallback logic."""
from __future__ import annotations


class SearchProviderError(RuntimeError):
    """A safe error suitable for CLI output; it must never contain a key."""

    def __init__(self, message: str, *, provider: str = "", native_error_type: str = "",
                 http_status: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.native_error_type = native_error_type
        self.http_status = http_status


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
