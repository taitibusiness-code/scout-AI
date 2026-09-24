from __future__ import annotations

"""VERIFY step: compare independently extracted, source-specific facts."""
from .models import (
    BusinessProfile, FactCheck, FactEvidence, SourceFact, SourceObservation,
    VerificationResult,
)


def _legacy_profile_facts(profiles: list[BusinessProfile]) -> list[SourceObservation]:
    """Support the old API without allowing aggregate profiles to verify."""
    observations: list[SourceObservation] = []
    for index, profile in enumerate(profiles):
        source_url = profile.extracted_from[0] if len(profile.extracted_from) == 1 else f"legacy-aggregate:{index}"
        confidence = "observed" if len(profile.extracted_from) == 1 else "inferred"
        values = {
            "business_name": profile.business_name,
            "location": profile.location,
            "what_they_sell": profile.what_they_sell,
            "has_ecommerce": "" if profile.has_ecommerce is None else str(profile.has_ecommerce),
        }
        facts = [SourceFact(field=k, value=v, source_url=source_url, confidence=confidence)
                 for k, v in values.items()]
        facts += [SourceFact(field=f"contact_channel:{channel}", value="present",
                             source_url=source_url, confidence=confidence)
                  for channel in profile.contact_channels]
        observations.append(SourceObservation(
            candidate_id=profile.candidate_id, source_url=source_url, fetched_at=profile.extracted_at,
            status_code=None, title=None, evidence_excerpt="", facts=facts,
        ))
    return observations


def verify(candidate_id: str, observations: list[SourceObservation] | list[BusinessProfile]) -> VerificationResult:
    """Produce fact checks with evidence provenance.

    Only direct (``observed``) source facts can be verified, and only when two
    distinct URLs independently support the same substantive value.
    """
    if observations and isinstance(observations[0], BusinessProfile):
        observations = _legacy_profile_facts(observations)  # type: ignore[arg-type]

    source_observations = observations  # type: ignore[assignment]
    source_urls = sorted({o.source_url for o in source_observations if not o.error})
    by_field: dict[str, list[SourceFact]] = {}
    for observation in source_observations:
        for source_fact in observation.facts:
            by_field.setdefault(source_fact.field, []).append(source_fact)

    expected = {"business_name", "location", "what_they_sell", "has_ecommerce"}
    expected.update(field for field in by_field if field.startswith("contact_channel:"))
    facts: list[FactCheck] = []
    conflicts: list[str] = []

    for field in sorted(expected):
        claims = [f for f in by_field.get(field, []) if f.value]
        direct = [f for f in claims if f.confidence == "observed" and f.source_url]
        values = {f.value for f in claims}
        direct_values = {f.value for f in direct}
        evidence = [FactEvidence(f.source_url, f.evidence_excerpt, f.extracted_at) for f in direct]
        sources = sorted({f.source_url for f in direct})

        if len(values) > 1:
            conflicts.append(f"{field} disagrees across sources: {', '.join(sorted(values))}")
            facts.append(FactCheck(field, "; ".join(sorted(values)), "observed" if direct else "inferred", sources, evidence))
        elif len(direct_values) == 1:
            value = next(iter(direct_values))
            confidence = "verified" if len(sources) >= 2 else "observed"
            facts.append(FactCheck(field, value, confidence, sources, evidence))
        else:
            inferred = [f for f in claims if f.confidence == "inferred"]
            if inferred:
                facts.append(FactCheck(field, inferred[0].value, "inferred", [], []))
            else:
                facts.append(FactCheck(field, "", "unknown", [], []))

    return VerificationResult(
        candidate_id=candidate_id, sources_checked=source_urls, facts=facts, conflicts=conflicts,
    )
