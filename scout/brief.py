from __future__ import annotations

"""RECOMMEND step: assemble everything into a ProspectBrief, rendered as
Markdown with the full evidence chain and per-fact confidence visible --
Dennis should never have to take a finding on faith."""
from .models import Candidate, BusinessProfile, VerificationResult, OpportunityFinding, ProspectBrief

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CONFIDENCE_ICON = {"verified": "✅", "observed": "◐", "inferred": "🔎", "unknown": "❔"}


def build_brief(candidate: Candidate, profile: BusinessProfile,
                 verification: VerificationResult,
                 opportunities: list[OpportunityFinding]) -> ProspectBrief:
    n_verified = sum(1 for f in verification.facts if f.confidence == "verified")
    summary = (
        f"{profile.business_name or candidate.name} -- {profile.what_they_sell or 'unclear offering'}. "
        f"Serves {profile.target_customers or 'unclear customer base'} in {profile.location or 'unclear location'}. "
        f"{len(opportunities)} opportunity(ies), {n_verified}/{len(verification.facts)} facts cross-source verified."
    )
    return ProspectBrief(
        candidate_id=candidate.id,
        business_name=profile.business_name or candidate.name,
        summary=summary,
        opportunities=opportunities,
        verification=verification,
        evidence_urls=profile.extracted_from,
    )


def to_markdown(brief: ProspectBrief) -> str:
    lines = [
        f"# Prospect Brief: {brief.business_name}",
        f"*Generated {brief.created_at} · status: {brief.status}*",
        "",
        "## Summary",
        brief.summary,
        "",
        "## Verification (per fact)",
    ]
    v = brief.verification
    if v:
        lines.append(f"Sources checked: {', '.join(v.sources_checked) or 'none'}")
        for f in v.facts:
            icon = CONFIDENCE_ICON.get(f.confidence, "?")
            lines.append(f"- {icon} **{f.fact}**: {f.value or '(not found)'} "
                          f"_({f.confidence}, {len(f.sources)} source(s))_")
            for e in f.evidence:
                lines.append(f"  - Evidence: {e.source_url} — {e.evidence_excerpt}")
        if v.conflicts:
            lines.append("")
            for c in v.conflicts:
                lines.append(f"- ⚠️ CONFLICT: {c}")
    lines += ["", "## Opportunities"]
    if not brief.opportunities:
        lines.append("_None identified from available evidence._")
    for o in sorted(brief.opportunities, key=lambda o: SEVERITY_ORDER.get(o.severity, 1)):
        icon = CONFIDENCE_ICON.get(o.confidence, "?")
        lines.append(f"### [{o.severity.upper()}] {o.category} {icon} {o.confidence}")
        lines.append(f"- **Observed:** {o.observed}")
        lines.append(f"- **Evidence:** {o.evidence_url}")
        if o.evidence_excerpt:
            lines.append(f"- **Evidence excerpt:** {o.evidence_excerpt}")
        lines.append(f"- **Business consequence:** {o.business_consequence}")
        lines.append(f"- **Opportunity:** {o.opportunity}")
        if o.alcatrax_capability:
            lines.append(f"- **Alcatrax capability:** {o.alcatrax_capability}")
        if o.reference_project:
            lines.append(f"- **Reference project:** {o.reference_project}")
        lines.append("")
    lines += ["## Evidence sources", *[f"- {u}" for u in brief.evidence_urls]]
    return "\n".join(lines)
