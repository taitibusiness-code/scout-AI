from __future__ import annotations

"""UNDERSTAND step: turn each raw PageSnapshot into source-specific facts.
Goes through an LLMProvider (see providers/base.py) -- this module never
imports `anthropic` directly, so swapping to local Ollama or another provider
touches zero lines here.
"""
from .models import PageSnapshot, BusinessProfile, SourceFact, SourceObservation
from .providers.base import LLMProvider

PROFILE_TOOL = {
    "name": "record_business_profile",
    "description": "Record structured facts extracted about a business from its web presence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "business_name": {"type": "string"},
            "what_they_sell": {"type": "string", "description": "Concrete, specific products/services."},
            "target_customers": {"type": "string"},
            "location": {"type": "string"},
            "contact_channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "e.g. phone, whatsapp, email, instagram, facebook",
            },
            "has_ecommerce": {"type": ["boolean", "null"]},
            "confidence_note": {
                "type": "string",
                "description": "Anything uncertain, missing, or inferred rather than stated on the page.",
            },
            "evidence": {
                "type": "object",
                "description": "A short verbatim excerpt from this source supporting each extracted field. Leave a field empty when the page does not directly support it.",
                "properties": {
                    "business_name": {"type": "string"},
                    "what_they_sell": {"type": "string"},
                    "target_customers": {"type": "string"},
                    "location": {"type": "string"},
                    "contact_channels": {"type": "string"},
                    "has_ecommerce": {"type": "string"},
                },
                "required": ["business_name", "what_they_sell", "target_customers", "location", "contact_channels", "has_ecommerce"],
            },
        },
        "required": ["business_name", "what_they_sell", "target_customers", "location",
                      "contact_channels", "has_ecommerce", "confidence_note", "evidence"],
    },
}

SYSTEM_PROMPT = (
    "You extract only what is actually stated or clearly implied on the page(s) "
    "given to you. For every non-empty field, provide a short verbatim excerpt "
    "from that same page in evidence. Do not invent details. If something isn't on the page, say so "
    "in confidence_note rather than guessing. You are looking at a small business's "
    "own website/listing, evaluated for a web agency's prospecting research -- "
    "accuracy matters more than completeness."
)


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _supported(excerpt: str, source_text: str) -> bool:
    """Only label a model field observed when its claimed quote is in the page."""
    return bool(excerpt and _normalise(excerpt) in _normalise(source_text))


def understand_source(candidate_id: str, snapshot: PageSnapshot, llm: LLMProvider) -> SourceObservation:
    """Extract one source only. Multiple URLs must never share one LLM claim."""
    source_excerpt = snapshot.text_content[:2000]
    if snapshot.error or not snapshot.text_content.strip():
        return SourceObservation(
            candidate_id=candidate_id, source_url=snapshot.url,
            fetched_at=snapshot.fetched_at, status_code=snapshot.status_code,
            title=snapshot.title, evidence_excerpt=source_excerpt, html_meta=snapshot.html_meta,
            error=snapshot.error or "No readable text was fetched.",
        )

    result = llm.extract(
        system=SYSTEM_PROMPT,
        user_content=(f"Extract a business profile from this one source only.\n"
                      f"URL: {snapshot.url}\nTITLE: {snapshot.title}\n"
                      f"META: {snapshot.html_meta}\nCONTENT:\n{snapshot.text_content}"),
        tool_name=PROFILE_TOOL["name"],
        tool_description=PROFILE_TOOL["description"],
        input_schema=PROFILE_TOOL["input_schema"],
        max_tokens=1000,
    )
    fields = result.input
    required = ("business_name", "what_they_sell", "target_customers", "location",
                "contact_channels", "has_ecommerce", "confidence_note", "evidence")
    text_fields = ("business_name", "what_they_sell", "target_customers", "location", "confidence_note")
    evidence_fields = ("business_name", "what_they_sell", "target_customers", "location",
                       "contact_channels", "has_ecommerce")
    if result.tool_name != PROFILE_TOOL["name"] or not isinstance(fields, dict) or any(k not in fields for k in required):
        raise ValueError("LLM extraction response is missing required evidence fields.")
    if any(not isinstance(fields[key], str) for key in text_fields):
        raise ValueError("LLM extraction response has invalid text fields.")
    if not isinstance(fields["contact_channels"], list) or not all(isinstance(x, str) for x in fields["contact_channels"]):
        raise ValueError("LLM extraction response has invalid contact_channels.")
    if fields["has_ecommerce"] not in (True, False, None):
        raise ValueError("LLM extraction response has invalid has_ecommerce.")
    if not isinstance(fields["evidence"], dict):
        raise ValueError("LLM extraction response has invalid evidence map.")
    if any(key not in fields["evidence"] or not isinstance(fields["evidence"][key], str)
           for key in evidence_fields):
        raise ValueError("LLM extraction response has incomplete evidence map.")

    def fact(field: str, value: object, evidence_key: str | None = None) -> SourceFact:
        rendered = "" if value is None else str(value)
        excerpt = fields["evidence"].get(evidence_key or field, "")
        if not isinstance(excerpt, str):
            raise ValueError(f"LLM extraction evidence for {field} is not text.")
        confidence = "unknown" if not rendered else ("observed" if _supported(excerpt, snapshot.text_content) else "inferred")
        return SourceFact(field=field, value=rendered, source_url=snapshot.url,
                          evidence_excerpt=excerpt[:600] if confidence == "observed" else "",
                          confidence=confidence)

    facts = [
        fact("business_name", fields["business_name"]),
        fact("what_they_sell", fields["what_they_sell"]),
        fact("target_customers", fields["target_customers"]),
        fact("location", fields["location"]),
        fact("has_ecommerce", fields["has_ecommerce"]),
    ]
    for channel in fields["contact_channels"]:
        facts.append(fact(f"contact_channel:{channel}", "present", "contact_channels"))
    return SourceObservation(
        candidate_id=candidate_id, source_url=snapshot.url, fetched_at=snapshot.fetched_at,
        status_code=snapshot.status_code, title=snapshot.title,
        evidence_excerpt=source_excerpt, html_meta=snapshot.html_meta, facts=facts,
    )


def profile_from_observations(candidate_id: str, observations: list[SourceObservation]) -> BusinessProfile:
    """Presentation-only profile; verification always uses source facts directly."""
    def first(field: str) -> str:
        for observation in observations:
            for source_fact in observation.facts:
                if source_fact.field == field and source_fact.value:
                    return source_fact.value
        return ""

    channels = [f.field[len("contact_channel:"):] for o in observations for f in o.facts
                if f.field.startswith("contact_channel:") and f.value]
    ecommerce = first("has_ecommerce")
    return BusinessProfile(
        candidate_id=candidate_id, business_name=first("business_name"),
        what_they_sell=first("what_they_sell"), target_customers=first("target_customers"),
        location=first("location"), contact_channels=list(dict.fromkeys(channels)),
        has_ecommerce={"True": True, "False": False}.get(ecommerce),
        extracted_from=[o.source_url for o in observations],
        confidence_note="Profile is a presentation summary; inspect source observations for evidence.",
    )


def understand(candidate_id: str, snapshots: list[PageSnapshot], llm: LLMProvider) -> BusinessProfile:
    """Compatibility helper. New pipeline code should use understand_source."""
    return profile_from_observations(candidate_id, [understand_source(candidate_id, s, llm) for s in snapshots])
