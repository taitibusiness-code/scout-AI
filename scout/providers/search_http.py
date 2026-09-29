"""Shared safe HTTP/error handling for public search adapters."""
from __future__ import annotations

import requests

from .search_errors import (SearchAuthenticationError, SearchRequestError,
                            TransientSearchError)


def get_json(provider: str, request) -> dict:
    """Make one mocked-or-live request and map errors without exposing secrets."""
    try:
        response = request()
    except requests.Timeout as error:
        raise TransientSearchError(f"{provider} search timed out.", provider=provider,
                                   native_error_type=type(error).__name__) from error
    except requests.ConnectionError as error:
        raise TransientSearchError(f"{provider} search connection failed.", provider=provider,
                                   native_error_type=type(error).__name__) from error
    except requests.RequestException as error:
        raise SearchRequestError(f"{provider} search request failed.", provider=provider,
                                 native_error_type=type(error).__name__) from error
    status = response.status_code
    if status in (401, 403):
        raise SearchAuthenticationError(f"{provider} search credentials were rejected (HTTP {status}).",
                                        provider=provider, http_status=status)
    if status == 429:
        raise TransientSearchError(f"{provider} search rate limit reached (HTTP 429).",
                                   provider=provider, http_status=status)
    if 500 <= status <= 599:
        raise TransientSearchError(f"{provider} search service failed (HTTP {status}).",
                                   provider=provider, http_status=status)
    if status >= 400:
        raise SearchRequestError(f"{provider} search request was rejected (HTTP {status}).",
                                 provider=provider, http_status=status)
    try:
        data = response.json()
    except (ValueError, TypeError) as error:
        raise SearchRequestError(f"{provider} search returned malformed JSON.", provider=provider,
                                 native_error_type=type(error).__name__) from error
    if not isinstance(data, dict):
        raise SearchRequestError(f"{provider} search returned an unexpected JSON shape.", provider=provider)
    return data
