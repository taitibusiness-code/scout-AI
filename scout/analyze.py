from __future__ import annotations

"""ANALYZE step / OPPORTUNITY ENGINE.

Produces full evidence chains, not scores:
observed -> evidence -> business_consequence -> opportunity -> alcatrax_capability
-> reference_project -> confidence

Grounded in scout/alcatrax_knowledge.py so the model picks real capabilities
and real past projects instead of inventing plausible-sounding ones. Mixes
cheap deterministic checks (zero hallucination risk) with one LLM pass for
judgment calls a regex can't make.
"""
from .models import PageSnapshot, BusinessProfile, OpportunityFinding
from .providers.base import LLMProvider
from . import alcatrax_knowledge as brain

OPPORTUNITIES_TOOL = {
    "name": "record_opportunities",
    "description": "Record specific, evidenced business opportunities as full reasoning chains.",
    "input_schema": {
        "type": "object",
        "properties": {
            "opportunities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["mobile_ux", "ecommerce", "seo", "whatsapp_flow",
                                      "stack", "trust_signals", "other"],
                        },
                        "observed": {"type": "string", "description": "What was actually seen. Fact, not judgment."},
                        "evidence_url": {"type": "string", "description": "Exact URL in the supplied material supporting observed."},
                        "evidence_excerpt": {"type": "string", "description": "Short verbatim excerpt from evidence_url supporting observed."},
                        "business_consequence": {"type": "string", "description": "Why this costs the business something concrete."},
                        "opportunity": {"type": "string", "description": "The specific thing Alcatrax could build."},
                        "alcatrax_capability": {
                            "type": "string",
                            "description": "Must be a capability id from the ALCATRAX CAPABILITIES list given to you, or empty string.",
                        },
                        "reference_project": {
                            "type": "string",
                            "description": "Must be a client name from the ALCATRAX PAST PROJECTS list given to you, or empty string if nothing genuinely matches.",
                        },
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "confidence": {
                            "type": "string",
                            "enum": ["observed", "verified", "inferred", "unknown"],
                            "description": "observed=seen directly, inferred=you concluded it rather than reading it directly.",
                        },
                    },
                    "required": ["category", "observed", "evidence_url", "evidence_excerpt", "business_consequence", "opportunity",
                                  "alcatrax_capability", "reference_project", "severity", "confidence"],
                },
            }
        },
        "required": ["opportunities"],
    },
}

SYSTEM_PROMPT_TEMPLATE = (
    "You are evaluating a business's digital presence for a Nairobi web agency "
    "(Alcatrax Technologies) deciding whether and how to pitch them. Only flag "
    "opportunities you can point to actual evidence for in the material given. "
    "Be specific -- 'no product catalogue visible on the homepage' not 'weak "
    "online presence'. Tag anything you concluded rather than read directly as "
    "confidence='inferred', never as 'observed'. Do not flag more than 5 "
    "opportunities; prioritize the clearest evidence and highest likely impact. Every finding "
    "must identify an exact supplied URL and a short verbatim excerpt that supports its observed field.\n\n"
    "{knowledge}"
)


def analyze(candidate_id: str, snapshots: list[PageSnapshot], profile: BusinessProfile,
            llm: LLMProvider) -> list[OpportunityFinding]:
    findings: list[OpportunityFinding] = []

    # Deterministic checks first -- cheap, no LLM call, no hallucination risk,
    # always confidence='observed' since they're read straight off the page.
    for s in snapshots:
        if s.error:
            continue
        if not s.html_meta.get("has_viewport_tag"):
            findings.append(OpportunityFinding(
                category="mobile_ux",
                observed="Page has no responsive viewport meta tag.",
                evidence_url=s.url,
                evidence_excerpt="HTML metadata: viewport tag absent.",
                business_consequence="Site likely renders desktop-only on phones, where most Kenyan traffic is.",
                opportunity="Responsive/mobile-first rebuild.",
                alcatrax_capability="websites",
                severity="high", confidence="observed",
            ))
        if not s.html_meta.get("meta_description"):
            findings.append(OpportunityFinding(
                category="seo",
                observed="No meta description tag set.",
                evidence_url=s.url,
                evidence_excerpt="HTML metadata: meta description absent.",
                business_consequence="Weak/blank Google search snippet, likely lower click-through from search.",
                opportunity="SEO-first metadata pass.",
                alcatrax_capability="seo",
                severity="medium", confidence="observed",
            ))
        if not s.html_meta.get("has_whatsapp_link") and "whatsapp" not in " ".join(profile.contact_channels).lower():
            findings.append(OpportunityFinding(
                category="whatsapp_flow",
                observed="No WhatsApp contact/chat link found on the page.",
                evidence_url=s.url,
                evidence_excerpt="HTML links: no wa.me or WhatsApp link found.",
                business_consequence="Customers likely default to a slower channel (phone tag, DM) to ask about stock/price.",
                opportunity="Pre-filled contextual WhatsApp CTA.",
                alcatrax_capability="whatsapp",
                severity="medium", confidence="observed",
            ))
        if profile.has_ecommerce is False and s.html_meta.get("has_ecommerce_words") is False:
            findings.append(OpportunityFinding(
                category="ecommerce",
                observed="No catalogue or online ordering/checkout flow detected.",
                evidence_url=s.url,
                evidence_excerpt="HTML/page-text heuristic: no ecommerce keywords detected.",
                business_consequence="Customers must contact manually to discover stock or prices.",
                opportunity="Searchable digital catalogue or full ecommerce flow.",
                alcatrax_capability="catalogue",
                severity="high", confidence="observed",
            ))

    # LLM pass, grounded in the real capability/project catalogue.
    pages_text = "\n\n---\n\n".join(
        f"URL: {s.url}\nCONTENT:\n{s.text_content}" for s in snapshots if not s.error
    )
    if pages_text.strip():
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(knowledge=brain.as_prompt_context())
        result = llm.extract(
            system=system_prompt,
            user_content=pages_text,
            tool_name=OPPORTUNITIES_TOOL["name"],
            tool_description=OPPORTUNITIES_TOOL["description"],
            input_schema=OPPORTUNITIES_TOOL["input_schema"],
            max_tokens=1200,
        )
        valid_capability_ids = {c.id for c in brain.CAPABILITIES}
        valid_project_names = {p.client_name for p in brain.PAST_PROJECTS}
        valid_categories = {"mobile_ux", "ecommerce", "seo", "whatsapp_flow", "stack", "trust_signals", "other"}
        valid_severities = {"low", "medium", "high"}
        valid_confidences = {"observed", "verified", "inferred", "unknown"}
        source_text = {s.url: s.text_content for s in snapshots if not s.error}

        def supported(url: str, excerpt: str) -> bool:
            return bool(url in source_text and excerpt and " ".join(excerpt.lower().split()) in
                        " ".join(source_text[url].lower().split()))

        if (result.tool_name != OPPORTUNITIES_TOOL["name"] or not isinstance(result.input, dict)
                or not isinstance(result.input.get("opportunities"), list)):
            return findings[:8]
        for item in result.input["opportunities"]:
            if not isinstance(item, dict):
                continue
            required = ("category", "observed", "evidence_url", "evidence_excerpt", "business_consequence",
                        "opportunity", "alcatrax_capability", "reference_project", "severity", "confidence")
            if any(key not in item or not isinstance(item[key], str) for key in required):
                continue
            if item["category"] not in valid_categories or item["severity"] not in valid_severities or item["confidence"] not in valid_confidences:
                continue
            if not supported(item["evidence_url"], item["evidence_excerpt"]):
                # An LLM statement without a traceable source is not an observed opportunity.
                continue
            # Guard rail: if the model names a capability/project that isn't
            # actually in the catalogue, drop the reference rather than trust
            # it -- this is exactly the "never invent a capability" rule from
            # the prompt, enforced in code, not just requested in the prompt.
            cap = item.get("alcatrax_capability", "")
            if cap not in valid_capability_ids:
                cap = ""
            ref = item.get("reference_project", "")
            if ref not in valid_project_names:
                ref = ""

            findings.append(OpportunityFinding(
                category=item["category"],
                observed=item["observed"],
                evidence_url=item["evidence_url"],
                evidence_excerpt=item["evidence_excerpt"][:600],
                business_consequence=item["business_consequence"],
                opportunity=item["opportunity"],
                alcatrax_capability=cap,
                reference_project=ref,
                severity=item["severity"],
                # One attributed source cannot establish a verified opportunity.
                confidence="observed" if item["confidence"] == "verified" else item["confidence"],
            ))

    return findings[:8]
