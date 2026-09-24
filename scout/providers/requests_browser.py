"""Plain requests + BeautifulSoup, wrapped to the BrowserProvider interface.
Same limitation as before: misses JS-rendered content. When that's a real
problem, add PlaywrightBrowserProvider in this same package implementing the
same .fetch() signature -- nothing outside providers/ changes."""
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from .base import BrowserProvider, FetchResult

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlcatraxScout/0.2; "
                  "+https://alcatrax.example/scout-bot)"
}


class RequestsBrowserProvider(BrowserProvider):
    def fetch(self, url: str, timeout: int = 15, max_chars: int = 8000) -> FetchResult:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
            return FetchResult(url=url, status_code=None, title=None,
                                text_content="", html_meta={}, error=str(e))

        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else None

        meta_desc = None
        tag = soup.find("meta", attrs={"name": "description"})
        if tag and tag.get("content"):
            meta_desc = tag["content"].strip()

        has_viewport = soup.find("meta", attrs={"name": "viewport"}) is not None
        whatsapp_link = (
            bool(soup.find("a", href=lambda h: h and "wa.me" in h)) or
            bool(soup.find("a", href=lambda h: h and "whatsapp" in h.lower()))
        )
        has_cart_words = any(
            w in resp.text.lower() for w in ["add to cart", "checkout", "shop now", "buy now"]
        )

        for tag_name in soup(["script", "style", "noscript"]):
            tag_name.decompose()
        text = " ".join(soup.get_text(separator=" ").split())[:max_chars]

        return FetchResult(
            url=url,
            status_code=resp.status_code,
            title=title,
            text_content=text,
            html_meta={
                "meta_description": meta_desc,
                "has_viewport_tag": has_viewport,
                "has_whatsapp_link": whatsapp_link,
                "has_ecommerce_words": has_cart_words,
            },
        )
