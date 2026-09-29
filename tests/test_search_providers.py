import os
import unittest
from unittest.mock import Mock, patch

from scout import config
from scout.providers.exa_search import ExaSearchProvider
from scout.providers.google_cse import GoogleCSEProvider
from scout.providers.search_errors import (SearchAuthenticationError,
                                           SearchConfigurationError,
                                           SearchRequestError,
                                           TransientSearchError)
from scout.providers.search_factory import FallbackSearchProvider, build_search_provider
from scout.providers.serper_search import SerperSearchProvider
from scout.providers.tavily_search import TavilySearchProvider


def response(status=200, data=None, json_error=None):
    value = Mock(status_code=status)
    if json_error:
        value.json.side_effect = json_error
    else:
        value.json.return_value = data if data is not None else {}
    return value


class SearchAdapterTests(unittest.TestCase):
    def test_exa_documented_results_mapping_and_malformed_rows(self):
        payload = {"results": [{"id": "exa-id", "title": "Exa title", "url": "https://exa.test", "highlights": ["first", "second"], "score": 0.9}, {"title": "bad"}]}
        with patch("scout.providers.exa_search.requests.post", return_value=response(data=payload)):
            hits = ExaSearchProvider("key").search("Nairobi", 3)
        self.assertEqual(1, len(hits)); self.assertEqual("first second", hits[0].snippet)
        self.assertEqual("exa", hits[0].metadata["provider"]); self.assertEqual("exa-id", hits[0].metadata["id"])

    def test_tavily_documented_results_mapping_and_malformed_rows(self):
        payload = {"results": [{"title": "Tavily title", "url": "https://tavily.test", "content": "summary", "score": 0.8}, {"url": None}]}
        with patch("scout.providers.tavily_search.requests.post", return_value=response(data=payload)):
            hits = TavilySearchProvider("key").search("Nairobi")
        self.assertEqual(1, len(hits)); self.assertEqual("summary", hits[0].snippet); self.assertEqual("tavily", hits[0].metadata["provider"])

    def test_serper_documented_organic_mapping_and_malformed_rows(self):
        payload = {"organic": [{"title": "Serper title", "link": "https://serper.test", "snippet": "summary", "position": 1}, {"link": 2}]}
        with patch("scout.providers.serper_search.requests.post", return_value=response(data=payload)):
            hits = SerperSearchProvider("key").search("Nairobi")
        self.assertEqual(1, len(hits)); self.assertEqual("summary", hits[0].snippet); self.assertEqual(1, hits[0].metadata["position"])

    def test_missing_keys_fail_clearly(self):
        for provider, label in ((ExaSearchProvider, "EXA_API_KEY"), (TavilySearchProvider, "TAVILY_API_KEY"), (SerperSearchProvider, "SERPER_API_KEY")):
            with self.subTest(provider=label), self.assertRaisesRegex(SearchConfigurationError, label): provider(None)

    def test_http_error_mapping_for_all_adapters(self):
        adapters = (("scout.providers.exa_search.requests.post", ExaSearchProvider), ("scout.providers.tavily_search.requests.post", TavilySearchProvider), ("scout.providers.serper_search.requests.post", SerperSearchProvider))
        for target, provider in adapters:
            for status, error_type in ((400, SearchRequestError), (401, SearchAuthenticationError), (403, SearchAuthenticationError), (429, TransientSearchError), (503, TransientSearchError)):
                with self.subTest(target=target, status=status), patch(target, return_value=response(status=status)):
                    with self.assertRaises(error_type): provider("key").search("Nairobi")

    def test_timeout_and_malformed_json_are_safe_errors(self):
        import requests
        with patch("scout.providers.exa_search.requests.post", side_effect=requests.Timeout):
            with self.assertRaises(TransientSearchError): ExaSearchProvider("key").search("Nairobi")
        with patch("scout.providers.tavily_search.requests.post", return_value=response(json_error=ValueError("bad"))):
            with self.assertRaises(SearchRequestError): TavilySearchProvider("key").search("Nairobi")

    def test_provider_error_does_not_echo_credential(self):
        secret = "not-for-output"
        with patch("scout.providers.serper_search.requests.post", return_value=response(status=401)):
            with self.assertRaises(SearchAuthenticationError) as caught:
                SerperSearchProvider(secret).search("Nairobi")
        self.assertNotIn(secret, str(caught.exception))

    def test_google_cse_mapping_still_skips_malformed_rows(self):
        payload = {"items": [{"title": "Google", "link": "https://google.test", "snippet": "result"}, {"link": None}]}
        with patch("scout.providers.google_cse.requests.get", return_value=response(data=payload)):
            hits = GoogleCSEProvider("key", "cx").search("Nairobi")
        self.assertEqual(1, len(hits)); self.assertEqual("google_cse", hits[0].metadata["provider"])


class FallbackTests(unittest.TestCase):
    def test_factory_defaults_to_exa_and_chain_takes_precedence(self):
        with patch.dict(os.environ, {"EXA_API_KEY": "key", "SEARCH_PROVIDER": "serper", "SEARCH_PROVIDERS": "exa,tavily", "TAVILY_API_KEY": "t"}, clear=True):
            cfg = config.load_config()
            provider = build_search_provider(cfg)
        self.assertEqual("FallbackSearchProvider", type(provider).__name__)
        self.assertEqual(["exa", "tavily"], [item.provider_name for item in provider.providers])

    def test_explicit_provider_has_no_silent_fallback(self):
        with patch.dict(os.environ, {"SEARCH_PROVIDER": "tavily", "EXA_API_KEY": "key"}, clear=True):
            with self.assertRaisesRegex(SearchConfigurationError, "TAVILY_API_KEY"):
                build_search_provider(config.load_config())

    def test_ordered_chain_stops_on_missing_first_provider_credential(self):
        with patch.dict(os.environ, {"SEARCH_PROVIDERS": "exa,tavily", "TAVILY_API_KEY": "key"}, clear=True):
            with self.assertRaisesRegex(SearchConfigurationError, "EXA_API_KEY"):
                build_search_provider(config.load_config())

    def test_ordered_fallback_succeeds_only_after_transient_error(self):
        first, second = Mock(provider_name="exa"), Mock(provider_name="tavily")
        first.search.side_effect = TransientSearchError("Exa search rate limit reached (HTTP 429).")
        second.search.return_value = ["ok"]
        self.assertEqual(["ok"], FallbackSearchProvider([first, second]).search("Nairobi"))
        second.search.assert_called_once()

    def test_fallback_does_not_continue_after_401_403_or_configuration_error(self):
        for error in (SearchAuthenticationError("Exa search credentials were rejected (HTTP 401)."), SearchAuthenticationError("Exa search credentials were rejected (HTTP 403)."), SearchConfigurationError("Exa search requires EXA_API_KEY.")):
            first, second = Mock(provider_name="exa"), Mock(provider_name="tavily")
            first.search.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                FallbackSearchProvider([first, second]).search("Nairobi")
            second.search.assert_not_called()

    def test_fallback_does_not_continue_after_invalid_query(self):
        first, second = Mock(provider_name="exa"), Mock(provider_name="tavily")
        with self.assertRaises(SearchRequestError):
            FallbackSearchProvider([first, second]).search(" ")
        first.search.assert_not_called(); second.search.assert_not_called()
