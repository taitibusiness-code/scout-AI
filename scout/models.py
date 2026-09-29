"""Data models for Scout. These are the schemas every pipeline step reads/writes,
and what gets persisted to storage. Keeping this explicit is what makes the
"remember" step and later cross-agent handoff (Developer, Sales) possible --
Developer/Sales don't need to re-derive what a business is, they read this.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


ENTITY_TYPES = ("prospect", "competitor", "industry_reference")
TARGET_PROFILES = ("local_sme", "corporate_operations", "unknown")
MISSION_STATUSES = ("PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "STOPPED")
TASK_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED")
TASK_TYPES = ("GENERATE_SEARCH", "SEARCH", "INVESTIGATE_ENTITY", "FETCH_PAGE", "RECON_ENTITY", "VERIFY_ENTITY", "EVALUATE_ENTITY", "REVISIT_ENTITY", "BUILD_REPORT")


@dataclass
class Entity:
    """Canonical organization identity; many discovery candidates may link here."""
    id: str = field(default_factory=_uid)
    canonical_name: str = ""
    entity_type: str = "prospect"
    primary_domain: str = ""
    industry: str = ""
    location: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class Mission:
    """Persisted intent and hard safety limits for autonomous public research."""
    id: str = field(default_factory=_uid)
    objective: str = ""
    status: str = "PENDING"
    geographic_scope: str = ""
    industries: list[str] = field(default_factory=list)
    business_types: list[str] = field(default_factory=list)
    target_profile: str = "local_sme"
    max_entities: int = 50
    max_searches: int = 20
    max_pages_per_entity: int = 3
    max_total_pages: int = 100
    max_retries: int = 2
    worker_count: int = 4
    time_budget_seconds: int | None = None
    freshness_seconds: int = 60 * 60 * 24 * 30
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    started_at: str = ""
    completed_at: str = ""
    paused_at: str = ""
    stop_reason: str = ""
    counters: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class ResearchTask:
    """A small, auditable unit of internal research; never an external action."""
    id: str = field(default_factory=_uid)
    mission_id: str = ""
    entity_id: str = ""
    parent_task_id: str = ""
    task_type: str = "SEARCH"
    status: str = "PENDING"
    priority: int = 50
    attempts: int = 0
    max_attempts: int = 2
    payload: dict = field(default_factory=dict)
    result_summary: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    started_at: str = ""
    completed_at: str = ""
    error: str = ""


@dataclass
class Candidate:
    """Output of the DISCOVER step: a raw lead before anything is verified."""
    id: str = field(default_factory=_uid)
    name: str = ""
    source: str = ""          # e.g. "google_cse", "manual"
    source_url: str = ""      # the search result URL that pointed at it
    query: str = ""           # the discover query that produced this
    entity_id: str = ""        # canonical Entity id, resolved after profile extraction
    campaign_status: str = "unknown"  # eligible | out_of_profile | unknown
    campaign_reason: str = ""
    discovered_at: str = field(default_factory=_now)


@dataclass
class PageSnapshot:
    """Output of the BROWSE step: raw evidence, kept verbatim for audit."""
    url: str
    fetched_at: str
    status_code: Optional[int]
    title: Optional[str]
    text_content: str          # cleaned visible text, truncated for LLM input
    html_meta: dict            # meta description, viewport tag, etc.
    error: Optional[str] = None


@dataclass
class BusinessProfile:
    """Output of the UNDERSTAND step: structured facts about the business,
    extracted from one or more PageSnapshots. This is a claim, not yet trusted."""
    candidate_id: str
    business_name: str
    what_they_sell: str
    target_customers: str
    location: str
    contact_channels: list[str]     # e.g. ["phone", "whatsapp", "email", "instagram"]
    has_ecommerce: Optional[bool]
    extracted_from: list[str]       # URLs used
    confidence_note: str            # model's own caveat, if any
    extracted_at: str = field(default_factory=_now)


CONFIDENCE_TIERS = ("observed", "verified", "inferred", "unknown")
# observed  = seen directly on one source, not cross-checked
# verified  = confirmed across 2+ independent sources
# inferred  = the model concluded this rather than reading it directly
# unknown   = looked for it, could not determine it


@dataclass
class SourceFact:
    """A claim extracted from exactly one fetched source.

    ``evidence_excerpt`` is deliberately bounded text rather than a full HTML
    document.  It lets a future reader audit the claim after the pipeline has
    finished without retaining an unnecessary copy of every page.
    """
    field: str
    value: str
    source_url: str
    evidence_excerpt: str = ""
    extracted_at: str = field(default_factory=_now)
    confidence: str = "unknown"


@dataclass
class SourceObservation:
    """Durable, source-specific research output for one fetched URL."""
    candidate_id: str
    source_url: str
    fetched_at: str
    status_code: Optional[int]
    title: Optional[str]
    evidence_excerpt: str
    html_meta: dict = field(default_factory=dict)
    facts: list[SourceFact] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class FactEvidence:
    """One source's support for an aggregated FactCheck."""
    source_url: str
    evidence_excerpt: str
    extracted_at: str


@dataclass
class FactCheck:
    """One specific claim about a business, tagged with how sure we are of it.
    This replaces a single whole-profile boolean -- confidence belongs to
    individual facts, not to the business as a whole."""
    fact: str                    # e.g. "has_ecommerce"
    value: str                   # the claimed value, as text
    confidence: str              # one of CONFIDENCE_TIERS
    sources: list[str] = field(default_factory=list)
    evidence: list[FactEvidence] = field(default_factory=list)


@dataclass
class VerificationResult:
    """Output of the VERIFY step: per-fact confidence, not one aggregate flag."""
    candidate_id: str
    sources_checked: list[str]
    facts: list[FactCheck] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)   # facts that disagree between sources
    checked_at: str = field(default_factory=_now)

    @property
    def verified(self) -> bool:
        """True only if at least one fact reached 'verified' and nothing conflicts.
        Kept as a property (not a stored field) so it can never drift out of
        sync with the actual per-fact data."""
        return len(self.conflicts) == 0 and any(f.confidence == "verified" for f in self.facts)


@dataclass
class OpportunityFinding:
    """One opportunity, as a full evidence chain -- not a score.
    observed -> evidence -> problem tags -> business consequence -> opportunity
    -> solution pattern -> capability -> reference project -> confidence
    """
    category: str                          # e.g. "mobile_ux", "ecommerce", "seo", "whatsapp_flow", "stack"
    observed: str                          # what was actually seen (fact, not judgment)
    evidence_url: str                      # where this was observed
    business_consequence: str              # why this costs the business something
    opportunity: str                       # the concrete thing Alcatrax could build
    evidence_excerpt: str = ""            # durable source excerpt / metadata observation
    problem_tags: list[str] = field(default_factory=list)
    solution_pattern: str = ""             # SolutionPattern id from alcatrax_knowledge
    alcatrax_capability: str = ""          # capability id from alcatrax_knowledge.CAPABILITIES
    reference_project: str = ""            # past project id/name this resembles, if any
    severity: str = "medium"               # "low" | "medium" | "high"
    confidence: str = "observed"           # one of CONFIDENCE_TIERS
    # Back-compat alias so anything still reading `.finding` doesn't break.
    @property
    def finding(self) -> str:
        return self.observed


@dataclass
class ProspectBrief:
    """Output of the RECOMMEND step: the deliverable Dennis actually reads."""
    id: str = field(default_factory=_uid)
    candidate_id: str = ""
    entity_id: str = ""
    business_name: str = ""
    summary: str = ""
    opportunities: list[OpportunityFinding] = field(default_factory=list)
    verification: Optional[VerificationResult] = None
    evidence_urls: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    status: str = "new"   # new | watching | dismissed | handed_off
