import unittest
from unittest.mock import Mock, patch

from scout.providers.google_places import DETAILS_FIELD_MASK, FIELD_MASK, GooglePlacesProvider, TEXT_SEARCH_URL
from scout.providers.search_errors import (SearchConfigurationError, SearchRequestError,
                                           TransientSearchError)


def response(status=200, data=None):
    value = Mock(status_code=status); value.json.return_value = data if data is not None else {}
    return value


class GooglePlacesProviderTests(unittest.TestCase):
    def test_text_search_uses_exact_minimum_field_mask_and_nairobi_bias(self):
        payload = {"places": [{"id": "place-id", "displayName": {"text": "Acme"}, "websiteUri": "https://acme.test", "formattedAddress": "Nairobi"}]}
        with patch("scout.providers.google_places.requests.post", return_value=response(data=payload)) as post:
            hits = GooglePlacesProvider("secret", True).search("hardware shops in Nairobi", 99)
        self.assertEqual(1, len(hits)); self.assertEqual("https://acme.test", hits[0].url)
        self.assertEqual("place-id", hits[0].metadata["place_id"])
        self.assertEqual(TEXT_SEARCH_URL, post.call_args.args[0])
        headers, body = post.call_args.kwargs["headers"], post.call_args.kwargs["json"]
        self.assertEqual(FIELD_MASK, headers["X-Goog-FieldMask"]); self.assertNotIn("secret", str(body))
        self.assertEqual(20, body["pageSize"]); self.assertEqual("KE", body["regionCode"])
        self.assertEqual(-1.286389, body["locationBias"]["circle"]["center"]["latitude"])
        self.assertNotIn("rating", FIELD_MASK); self.assertNotIn("review", FIELD_MASK)

    def test_disabled_and_missing_key_fail_without_key_output(self):
        with self.assertRaisesRegex(SearchConfigurationError, "disabled"):
            GooglePlacesProvider("secret", False)
        with self.assertRaisesRegex(SearchConfigurationError, "GOOGLE_MAPS_API_KEY"):
            GooglePlacesProvider(None, True)

    def test_maps_only_hit_has_no_browsable_url(self):
        payload = {"places": [{"id": "place-only", "displayName": {"text": "Maps only"}, "googleMapsUri": "https://maps.google.com/x"}]}
        with patch("scout.providers.google_places.requests.post", return_value=response(data=payload)):
            hit = GooglePlacesProvider("key", True).search("salon in Nairobi")[0]
        self.assertEqual("", hit.url); self.assertTrue(hit.metadata["maps_only"])

    def test_transient_and_non_transient_errors_follow_shared_safe_mapping(self):
        with patch("scout.providers.google_places.requests.post", return_value=response(status=429)):
            with self.assertRaises(TransientSearchError): GooglePlacesProvider("key", True).search("x")
        with patch("scout.providers.google_places.requests.post", return_value=response(status=400)):
            with self.assertRaises(SearchRequestError): GooglePlacesProvider("key", True).search("x")

    def test_details_lookup_requests_only_website_uri(self):
        with patch("scout.providers.google_places.requests.get", return_value=response(data={"websiteUri": "https://first-party.test"})) as get:
            self.assertEqual("https://first-party.test", GooglePlacesProvider("key", True).website_uri("place/id"))
        self.assertEqual("https://places.googleapis.com/v1/places/place%2Fid", get.call_args.args[0])
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(DETAILS_FIELD_MASK, headers["X-Goog-FieldMask"])
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("key", headers["X-Goog-Api-Key"])

    def test_empty_details_website_is_not_a_candidate_url(self):
        with patch("scout.providers.google_places.requests.get", return_value=response(data={})):
            self.assertEqual("", GooglePlacesProvider("key", True).website_uri("place-id"))
