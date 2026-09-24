"""BROWSE step. Goes through a BrowserProvider -- never imports requests
directly. See providers/requests_browser.py for the default backend, and its
docstring for when/how to add a Playwright-based provider for JS-heavy sites.
"""
from .models import PageSnapshot
from .providers.base import BrowserProvider


def fetch(url: str, browser: BrowserProvider, timeout: int = 15, max_chars: int = 8000) -> PageSnapshot:
    result = browser.fetch(url, timeout=timeout, max_chars=max_chars)
    return PageSnapshot(
        url=result.url, fetched_at=_now(), status_code=result.status_code,
        title=result.title, text_content=result.text_content,
        html_meta=result.html_meta, error=result.error,
    )


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
