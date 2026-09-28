"""Conservative public-web browser provider with URL and robots.txt safeguards."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .base import BrowserProvider, FetchResult

USER_AGENT = "AlcatraxScout/0.2 (+https://alcatrax.example/scout-bot)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
ROBOTS_CACHE_TTL = timedelta(hours=6)
MAX_REDIRECTS = 5


class UnsafeURL(ValueError):
    """Raised before a request can reach a non-public destination."""


class RobotsPolicyError(UnsafeURL):
    """Raised before following a redirect that robots policy does not permit."""


def validate_public_url(url: str) -> str:
    """Return a normalized public HTTP(S) URL, rejecting unsafe destinations.

    DNS is resolved before each request and every answer must be globally
    routable. This is a conservative SSRF guard; the normal DNS-rebinding
    limitation of high-level HTTP clients still applies.
    """
    try:
        parsed = urlsplit((url or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise UnsafeURL("only absolute public http/https URLs are permitted")
        if parsed.username or parsed.password:
            raise UnsafeURL("URLs with embedded credentials are not permitted")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise UnsafeURL("invalid URL port")
        host = parsed.hostname.rstrip(".").lower()
    except (ValueError, AttributeError) as error:
        raise UnsafeURL("malformed URL") from error
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURL("localhost is not a public destination")
    try:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
    except socket.gaierror as error:
        raise UnsafeURL("host could not be resolved to a public destination") from error
    if not addresses:
        raise UnsafeURL("host could not be resolved to a public destination")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise UnsafeURL("non-public IP destinations are not permitted")
        except ValueError as error:
            raise UnsafeURL("invalid resolved address") from error
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class RequestsBrowserProvider(BrowserProvider):
    """Fetch public HTML only; no cookies/auth headers or page bodies are logged."""
    def __init__(self):
        self._robots_cache: dict[str, tuple[datetime, RobotFileParser | None]] = {}

    def _robots_allowed(self, url: str) -> tuple[bool, str]:
        parsed = urlsplit(url)
        key = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots_cache.get(key)
        if cached and cached[0] > datetime.now(timezone.utc):
            parser = cached[1]
        else:
            robots_url = validate_public_url(urljoin(key + "/", "robots.txt"))
            try:
                response, _ = self._request_with_safe_redirects(robots_url, timeout=10)
                if response.status_code >= 400:
                    parser = None
                else:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
            except (requests.RequestException, UnsafeURL):
                parser = None
            self._robots_cache[key] = (datetime.now(timezone.utc) + ROBOTS_CACHE_TTL, parser)
        # Unknown/unavailable robots policy is skipped conservatively rather
        # than guessed; a caller can retry later when the policy is reachable.
        if parser is None:
            return False, "ROBOTS_UNAVAILABLE"
        return (parser.can_fetch(USER_AGENT, url), "ROBOTS_DISALLOWED")

    def _request_with_safe_redirects(self, url: str, timeout: int, on_redirect=None):
        current = validate_public_url(url)
        for index in range(MAX_REDIRECTS + 1):
            response = requests.get(current, headers=HEADERS, timeout=timeout, allow_redirects=False)
            if not 300 <= response.status_code < 400 or not response.headers.get("Location"):
                return response, current
            if index == MAX_REDIRECTS:
                raise UnsafeURL("too many redirects")
            current = validate_public_url(urljoin(current, response.headers["Location"]))
            if on_redirect:
                on_redirect(current)
        raise UnsafeURL("too many redirects")

    def fetch(self, url: str, timeout: int = 15, max_chars: int = 8000) -> FetchResult:
        try:
            safe_url = validate_public_url(url)
            allowed, policy_error = self._robots_allowed(safe_url)
            if not allowed:
                return FetchResult(url=safe_url, status_code=None, title=None, text_content="", html_meta={}, error=policy_error)
            def check_redirect_policy(destination: str) -> None:
                allowed, policy_error = self._robots_allowed(destination)
                if not allowed:
                    raise RobotsPolicyError(policy_error)
            resp, final_url = self._request_with_safe_redirects(safe_url, timeout, check_redirect_policy)
        except RobotsPolicyError as error:
            return FetchResult(url=url, status_code=None, title=None, text_content="", html_meta={}, error=str(error))
        except (requests.RequestException, UnsafeURL) as error:
            return FetchResult(url=url, status_code=None, title=None, text_content="", html_meta={}, error=f"UNSAFE_OR_NETWORK_URL: {error}")

        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        tag = soup.find("meta", attrs={"name": "description"})
        meta_desc = tag["content"].strip() if tag and tag.get("content") else None
        has_viewport = soup.find("meta", attrs={"name": "viewport"}) is not None
        whatsapp_link = bool(soup.find("a", href=lambda h: h and ("wa.me" in h or "whatsapp" in h.lower())))
        body_lower = resp.text.lower()
        canonical_tag = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
        visible_before_cleanup = soup.get_text(" ")
        for tag_name in soup(["script", "style", "noscript"]):
            tag_name.decompose()
        text = " ".join(soup.get_text(separator=" ").split())[:max_chars]
        return FetchResult(url=final_url, status_code=resp.status_code, title=title, text_content=text, html_meta={
            "meta_description": meta_desc,
            "has_viewport_tag": has_viewport,
            "has_whatsapp_link": whatsapp_link,
            "has_ecommerce_words": any(w in body_lower for w in ["add to cart", "checkout", "shop now", "buy now"]),
            "canonical": canonical_tag.get("href", "") if canonical_tag else "",
            "links": [anchor.get("href") for anchor in soup.find_all("a", href=True)][:200],
            "phone_numbers": list(dict.fromkeys(re.findall(r"(?:\+?\d[\d\s().-]{7,}\d)", visible_before_cleanup)))[:10],
            "emails": list(dict.fromkeys(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", visible_before_cleanup)))[:10],
            "has_json_ld": bool(soup.find("script", attrs={"type": "application/ld+json"})),
            "has_analytics_hint": any(token in body_lower for token in ("googletagmanager", "google-analytics", "gtag(")),
        })
