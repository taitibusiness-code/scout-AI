from __future__ import annotations

"""The Scout pipeline: discover -> browse -> understand -> verify -> analyze
-> remember -> recommend.

Now takes providers explicitly (see providers/base.py) rather than reaching
for a specific API -- this function has no idea whether it's talking to
Google CSE or another search backend, Anthropic or local Ollama.

Still sequential by design. See README for why this isn't wrapped in
orchestrator-worker multi-agent machinery yet.
"""
from dataclasses import dataclass, field
from .models import Candidate
from .providers.base import SearchProvider, BrowserProvider, LLMProvider
from . import discover, browse, extract, verify, analyze, brief as brief_mod, store
from .logging_utils import ActionLog


@dataclass
class PipelineRunResult:
    briefs: list[dict] = field(default_factory=list)
    discovered: int = 0
    failures: list[dict] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return len(self.briefs)


def run(query: str, search: SearchProvider, browser: BrowserProvider, llm: LLMProvider,
        db_path: str, log_path: str, max_candidates: int = 10,
        extra_sources_per_candidate: list[str] | None = None, entity_type: str = "prospect",
        industry: str = "") -> PipelineRunResult:
    log = ActionLog(log_path)
    outcome = PipelineRunResult()
    store.validate_entity_type(entity_type)

    # DISCOVER
    candidates = discover.discover_search(query, search, max_candidates)
    outcome.discovered = len(candidates)
    log.log("discover", None, "query_run", {"query": query, "found": len(candidates)})

    for candidate in candidates:
        try:
            store.save_candidate(db_path, candidate)

            # BROWSE
            urls = list(dict.fromkeys([candidate.source_url] + (extra_sources_per_candidate or [])))
            snapshots = [browse.fetch(u, browser) for u in urls if u]
            log.log("browse", candidate.id, "fetched",
                    {"urls": urls, "errors": [s.error for s in snapshots if s.error]})

            # UNDERSTAND
            observations = [extract.understand_source(candidate.id, snapshot, llm) for snapshot in snapshots]
            for observation in observations:
                store.save_source_observation(db_path, observation)
            profile = extract.profile_from_observations(candidate.id, observations)
            store.save_profile(db_path, profile)
            log.log("understand", candidate.id, "extracted", {"business_name": profile.business_name})

            # Resolve only after source-specific extraction supplies a factual
            # location/name. Candidates remain individual discovery occurrences.
            entity = store.resolve_entity(
                db_path, profile.business_name or candidate.name, candidate.source_url,
                profile.location, entity_type=entity_type, industry=industry,
            )
            store.link_candidate_to_entity(db_path, candidate, entity.id)
            log.log("resolve_entity", candidate.id, "linked", {"entity_id": entity.id, "entity_type": entity.entity_type})

            # VERIFY (pass extra_sources_per_candidate to get real cross-source
            # verification -- with one source, facts cap out at 'observed')
            verification = verify.verify(candidate.id, observations)
            store.save_verification(db_path, verification)
            log.log("verify", candidate.id, "checked",
                    {"verified_facts": sum(1 for f in verification.facts if f.confidence == "verified")})

            # ANALYZE (opportunity engine, grounded in alcatrax_knowledge)
            opportunities = analyze.analyze(candidate.id, snapshots, profile, llm)
            log.log("analyze", candidate.id, "opportunities_found", {"count": len(opportunities)})

            # RECOMMEND
            brief = brief_mod.build_brief(candidate, profile, verification, opportunities)

            # REMEMBER
            store.save_brief(db_path, brief)
            log.log("remember", candidate.id, "brief_saved", {"brief_id": brief.id})

            outcome.briefs.append({"brief": brief, "markdown": brief_mod.to_markdown(brief)})

        except Exception as e:  # one bad candidate must not kill the run
            log.log("pipeline", candidate.id, "error", {"error": str(e)})
            outcome.failures.append({"candidate_id": candidate.id, "candidate_name": candidate.name, "error": str(e)})
            continue

    return outcome
