"""Official Google Places API (New) Text Search discovery adapter.

This adapter deliberately does not fetch Google Maps URLs.  Places listing data
is transient discovery context; a result is usable by the mission only when it
has a public first-party website that Scout can inspect under its normal web
and robots safeguards.
"""
from __future__ import annotations

import requests
from urllib.parse import quote

from .base import SearchHit, SearchProvider
from .search_errors import SearchConfigurationError, SearchRequestError
from .search_http import get_json

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_FIELD_MASK = "websiteUri"
# The deliberately small field mask excludes photos, reviews, ratings and AI
# generated summaries.  It is also asserted by tests to prevent cost creep.
FIELD_MASK = ",".join((
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
    "places.businessStatus", "places.googleMapsUri",
))
NAIROBI_BIAS = {"circle": {"center": {"latitude": -1.286389, "longitude": 36.817223}, "radius": 30000.0}}


class GooglePlacesProvider(SearchProvider):
    provider_name = "google_places"

    def __init__(self, api_key: str | None, enabled: bool = False):
        if not enabled:
            raise SearchConfigurationError("Google Places discovery is disabled. Set PLACES_PROVIDER_ENABLED=true to enable it.", provider="Google Places")
        if not api_key:
            raise SearchConfigurationError("Google Places discovery requires GOOGLE_MAPS_API_KEY.", provider="Google Places")
        self.api_key = api_key

    def search(self, query: str, num_results: int = 10) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("Google Places search query must be non-empty.", provider="Google Places")
        data = get_json("Google Places", lambda: requests.post(
            TEXT_SEARCH_URL,
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"},
            json={"textQuery": query.strip(), "locationBias": NAIROBI_BIAS,
                  "regionCode": "KE", "pageSize": min(max(num_results, 1), 20)}, timeout=20,
        ))
        rows = data.get("places", [])
        if not isinstance(rows, list):
            raise SearchRequestError("Google Places search returned an unexpected results shape.", provider="Google Places")
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            name = row.get("displayName", {})
            title = name.get("text", "") if isinstance(name, dict) else ""
            website = row.get("websiteUri", "")
            # Never hand a Google Maps URI to the browser.  A maps-only listing
            # remains transient and cannot become a researched candidate.
            url = website.strip() if isinstance(website, str) else ""
            metadata = {"provider": self.provider_name, "place_id": row["id"],
                        "maps_only": not bool(url)}
            # These values are intentionally transient: mission persistence
            # stores only place_id/provenance until first-party web evidence exists.
            metadata["places_identity"] = {key: row.get(key) for key in (
                "formattedAddress", "nationalPhoneNumber", "internationalPhoneNumber", "businessStatus", "googleMapsUri", "location")}
            hits.append(SearchHit(title=title if isinstance(title, str) else "", url=url, snippet="", metadata=metadata))
        return hits

    def website_uri(self, place_id: str) -> str:
        """Resolve only a website URI transiently; never persist this response."""
        if not place_id:
            raise SearchRequestError("Google Places place ID is required.", provider="Google Places")
        data = get_json("Google Places", lambda: requests.get(
            f"https://places.googleapis.com/v1/places/{quote(place_id, safe='')}",
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": self.api_key,
                     "X-Goog-FieldMask": DETAILS_FIELD_MASK}, timeout=20,
        ))
        value = data.get("websiteUri", "")
        return value.strip() if isinstance(value, str) else ""
