"""Maintainable Alcatrax market territory used by deterministic mission planning."""
from __future__ import annotations

MARKET_TAXONOMY = {
    "automotive": ("spare parts shops", "car dealerships", "garages"),
    "hospitality": ("hotels", "restaurants"),
    "health_fitness": ("salons", "barbershops", "gyms", "clinics"),
    "education_professional": ("schools", "law firms", "real estate agents", "professional portfolios"),
    "retail_local": ("supermarkets", "boutiques", "electronics shops", "hardware shops", "startups", "SMEs"),
    "construction_services": ("construction companies", "cleaning companies", "Jua Kali workshops"),
}

def normalize_industries(industries: list[str] | None) -> list[str]:
    if not industries:
        return list(MARKET_TAXONOMY)
    unknown = [value for value in industries if value not in MARKET_TAXONOMY]
    if unknown:
        raise ValueError(f"Unknown industries: {', '.join(unknown)}")
    return list(dict.fromkeys(industries))

def business_types_for(industries: list[str]) -> list[str]:
    return [item for industry in industries for item in MARKET_TAXONOMY[industry]]
