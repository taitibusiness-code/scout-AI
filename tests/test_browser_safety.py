import socket
import unittest
from unittest.mock import patch

from scout.providers.requests_browser import RequestsBrowserProvider, UnsafeURL, validate_public_url


PUBLIC_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class Response:
    def __init__(self, status=200, text="", location=None):
        self.status_code, self.text = status, text
        self.headers = {"Location": location} if location else {}


class BrowserSafetyTests(unittest.TestCase):
    def test_non_public_and_malformed_urls_are_rejected(self):
        for url in ("", "ftp://example.test", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            with self.assertRaises(UnsafeURL, msg=url): validate_public_url(url)

    @patch("scout.providers.requests_browser.socket.getaddrinfo", return_value=PUBLIC_ADDRESS)
    def test_public_url_is_normalized_without_fragment(self, _resolver):
        self.assertEqual("https://example.test/path?q=1", validate_public_url("https://Example.TEST/path?q=1#fragment"))

    @patch("scout.providers.requests_browser.socket.getaddrinfo", return_value=PUBLIC_ADDRESS)
    @patch("scout.providers.requests_browser.requests.get")
    def test_unsafe_redirect_is_rejected_before_following(self, request, _resolver):
        request.side_effect = [Response(200, "User-agent: *\nAllow: /"), Response(302, location="http://localhost/private")]
        result = RequestsBrowserProvider().fetch("https://example.test/start")
        self.assertIn("UNSAFE_OR_NETWORK_URL", result.error)
        self.assertEqual(2, request.call_count)

    @patch("scout.providers.requests_browser.socket.getaddrinfo", return_value=PUBLIC_ADDRESS)
    @patch("scout.providers.requests_browser.requests.get")
    def test_redirect_destination_robots_policy_is_checked_before_fetch(self, request, _resolver):
        request.side_effect = [
            Response(200, "User-agent: *\nAllow: /"),
            Response(302, location="https://other.test/private"),
            Response(200, "User-agent: *\nDisallow: /private"),
        ]
        result = RequestsBrowserProvider().fetch("https://example.test/start")
        self.assertEqual("ROBOTS_DISALLOWED", result.error)
        self.assertEqual(3, request.call_count)

    @patch("scout.providers.requests_browser.socket.getaddrinfo", return_value=PUBLIC_ADDRESS)
    @patch("scout.providers.requests_browser.requests.get")
    def test_robots_disallow_skips_page_and_is_cached(self, request, _resolver):
        request.return_value = Response(200, "User-agent: *\nDisallow: /private")
        browser = RequestsBrowserProvider()
        first = browser.fetch("https://example.test/private/a")
        second = browser.fetch("https://example.test/private/b")
        self.assertEqual("ROBOTS_DISALLOWED", first.error); self.assertEqual("ROBOTS_DISALLOWED", second.error)
        self.assertEqual(1, request.call_count)
