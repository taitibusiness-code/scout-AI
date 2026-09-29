"""Offline-first autonomous research loop. It only performs public research."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic, sleep
from urllib.parse import urlparse, urljoin
import re
import random
import threading

from . import store
from .models import Candidate, Mission, ResearchTask, SourceFact, SourceObservation, BusinessProfile, VerificationResult, OpportunityFinding, TARGET_PROFILES
from . import alcatrax_knowledge as brain, brief as brief_mod, intelligence
from .taxonomy import business_types_for, normalize_industries
from .providers.search_errors import SearchProviderError, TransientSearchError


class DomainPoliteness:
    """Thread-safe per-host spacing; independent hosts never block one another."""
    def __init__(self, spacing_seconds: float = 0.25):
        self.spacing_seconds, self._last, self._lock = spacing_seconds, {}, threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            delay = max(0.0, self._last.get(host, 0) + self.spacing_seconds - monotonic())
            self._last[host] = monotonic() + delay
        if delay:
            sleep(delay)


class HTTPTaskError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.transient = status_code in (408, 429, 500, 502, 503, 504)


class PolicySkipped(RuntimeError):
    """A public-web policy prevented the fetch; this is not a task failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_queries(mission: Mission, completed_queries: set[str] | None = None) -> list[tuple[str, str]]:
    """Generate bounded, non-duplicated search angles without any LLM."""
    completed_queries = completed_queries or set()
    rows = []
    for industry in normalize_industries(mission.industries):
        for business_type in mission.business_types or business_types_for([industry]):
            if business_type not in business_types_for([industry]):
                continue
            query = " ".join(part for part in (business_type, mission.geographic_scope) if part).strip()
            if query.lower() not in {value.lower() for value in completed_queries}:
                rows.append((industry, query))
    return rows


def prioritise_pages(homepage: str, links: list[str], limit: int) -> list[str]:
    """Only same-domain, useful HTML-ish routes; homepage always comes first."""
    host = urlparse(homepage).netloc.lower()
    ranked = []
    weights = (("contact", 90), ("about", 80), ("shop", 75), ("product", 75), ("catalog", 75),
               ("service", 65), ("booking", 60), ("reservation", 60), ("project", 55), ("portfolio", 55))
    for raw in links:
        url = urljoin(homepage, raw)
        parsed = urlparse(url)
        if parsed.netloc.lower() != host or parsed.scheme not in ("http", "https"):
            continue
        path = parsed.path.lower()
        if any(token in path for token in ("login", "account", "wp-admin", ".pdf", ".jpg", ".png", "page=")):
            continue
        score = next((score for token, score in weights if token in path), 0)
        if score:
            ranked.append((score, url.split("#")[0]))
    output = [homepage]
    for _, url in sorted(ranked, key=lambda row: (-row[0], row[1])):
        if url not in output:
            output.append(url)
        if len(output) >= limit:
            break
    return output


class ScoutMissionEngine:
    """A resumable, single-machine task runner. No outreach or external writes."""
    def __init__(self, db_path: str, search_provider=None, browser_provider=None, politeness: DomainPoliteness | None = None):
        self.db_path, self.search, self.browser = db_path, search_provider, browser_provider
        self.politeness = politeness or DomainPoliteness()
        self._persist_lock = threading.Lock()

    def create(self, objective: str, location: str = "", industries: list[str] | None = None,
               business_types: list[str] | None = None, target_profile: str = "local_sme", **limits) -> Mission:
        selected = normalize_industries(industries)
        self._validate_limits(limits)
        if target_profile not in TARGET_PROFILES:
            raise ValueError(f"target_profile must be one of: {', '.join(TARGET_PROFILES)}")
        allowed_types = business_types_for(selected)
        selected_types = list(dict.fromkeys(business_types or allowed_types))
        unknown_types = [item for item in selected_types if item not in allowed_types]
        if not selected_types or unknown_types:
            raise ValueError("business_types must be non-empty and belong to the selected industries")
        mission = Mission(objective=objective, geographic_scope=location, industries=selected,
                          business_types=selected_types, target_profile=target_profile, **limits)
        store.save_mission(self.db_path, mission)
        store.log_mission_event(self.db_path, mission.id, "MISSION_CREATED", {"objective": objective})
        for industry, query in plan_queries(mission):
            store.save_task(self.db_path, ResearchTask(mission_id=mission.id, task_type="SEARCH", priority=50,
                                                        max_attempts=mission.max_retries + 1,
                                                        payload={"query": query, "industry": industry}))
            store.log_mission_event(self.db_path, mission.id, "SEARCH_CREATED", {"query": query})
        return mission

    def pause(self, mission_id: str, reason: str = "paused by operator") -> Mission:
        mission = self._mission(mission_id)
        mission.status, mission.paused_at, mission.stop_reason = "PAUSED", _now(), reason
        store.save_mission(self.db_path, mission); store.log_mission_event(self.db_path, mission.id, "MISSION_PAUSED", {"reason": reason})
        return mission

    def run(self, mission_id: str) -> Mission:
        mission = self._mission(mission_id)
        if mission.status not in ("PENDING", "PAUSED", "RUNNING"):
            return mission
        if self.search is None or self.browser is None:
            raise RuntimeError("A SearchProvider and BrowserProvider are required to run a mission; planning/reporting need neither.")
        mission.status, mission.started_at, mission.stop_reason = "RUNNING", mission.started_at or _now(), ""
        # A process interruption may leave a task marked RUNNING. It was never
        # completed, so make it eligible again rather than silently losing work.
        for orphan in store.tasks_for_mission(self.db_path, mission.id, "RUNNING"):
            orphan.status, orphan.error = "PENDING", "recovered after interrupted run"
            store.save_task(self.db_path, orphan)
        store.save_mission(self.db_path, mission); store.log_mission_event(self.db_path, mission.id, "MISSION_RESUMED" if mission.paused_at else "MISSION_STARTED")
        started = monotonic()
        while mission.status == "RUNNING":
            if mission.time_budget_seconds and monotonic() - started >= mission.time_budget_seconds:
                self._finish(mission, "STOPPED", "time budget reached"); break
            pending = store.tasks_for_mission(self.db_path, mission.id, "PENDING")
            if not pending:
                self._finish(mission, "COMPLETED", "all planned work completed"); break
            eligible = [task for task in pending if not self._limit_reached(mission, task)]
            if not eligible:
                self._finish(mission, "STOPPED", "mission limit reached"); break
            batch, domains = [], set()
            search_slots = max(0, mission.max_searches - mission.counters.get("searches_executed", 0))
            page_slots = max(0, mission.max_total_pages - mission.counters.get("pages_fetched", 0))
            for task in eligible:
                if task.task_type == "SEARCH":
                    # A retry belongs to an already-admitted search task; only a
                    # first attempt consumes the hard logical-search budget.
                    if not task.attempts:
                        if not search_slots: continue
                        search_slots -= 1
                if task.task_type == "FETCH_PAGE":
                    if not page_slots: continue
                    page_slots -= 1
                domain = store.normalize_domain(task.payload.get("url", "")) if task.task_type == "FETCH_PAGE" else ""
                if domain and domain in domains:
                    continue
                batch.append(task); domains.add(domain)
                if len(batch) >= mission.worker_count:
                    break
            with ThreadPoolExecutor(max_workers=mission.worker_count) as workers:
                futures = [workers.submit(self._execute, mission, task) for task in batch]
                for future in as_completed(futures):
                    future.result()  # each task handles its own failure; unexpected errors stay visible
            mission = self._mission(mission.id)
        self.build_report(mission.id)
        return self._mission(mission.id)

    def _execute(self, mission: Mission, task: ResearchTask) -> None:
        task.status, task.started_at, task.attempts = "RUNNING", _now(), task.attempts + 1; store.save_task(self.db_path, task)
        if task.task_type == "SEARCH" and task.attempts == 1:
            self._count(mission, "searches_executed")
        try:
            if task.task_type == "SEARCH": self._search_task(mission, task)
            elif task.task_type == "FETCH_PAGE": self._fetch_task(mission, task)
            elif task.task_type in ("INVESTIGATE_ENTITY", "REVISIT_ENTITY"): self._investigate_task(mission, task)
            elif task.task_type == "EVALUATE_ENTITY": self._evaluate_task(mission, task)
            else: task.result_summary = {"skipped": "not needed in deterministic pass"}
            task.status, task.completed_at, task.error = "COMPLETED", _now(), ""
        except PolicySkipped as error:
            task.status, task.completed_at, task.error = "SKIPPED", _now(), str(error)
            task.result_summary = {"skipped_policy": str(error)}
        except HTTPTaskError as error:
            task.error = str(error)
            retry = error.transient and task.attempts < task.max_attempts
            self._record_failure_diagnostic(task, error, retry)
            if retry:
                task.status, task.priority = "PENDING", task.priority - 1
                sleep(min(0.05 * (2 ** (task.attempts - 1)) + random.random() * 0.01, 0.2))
            else:
                task.status, task.completed_at = "FAILED", _now(); self._count(mission, "failures")
        except Exception as error:
            retry = isinstance(error, TransientSearchError) and task.attempts < task.max_attempts
            task.error = str(error) if isinstance(error, SearchProviderError) else "Task failed unexpectedly."
            self._record_failure_diagnostic(task, error, retry)
            if retry:
                task.status = "PENDING"; task.priority -= 1
                sleep(min(0.05 * (2 ** (task.attempts - 1)) + random.random() * 0.01, 0.2))
            else:
                task.status, task.completed_at = "FAILED", _now()
                self._count(mission, "failures")
        store.save_task(self.db_path, task)

    def _record_failure_diagnostic(self, task: ResearchTask, error: Exception, retry: bool) -> None:
        """Persist only safe, structured failure facts; never exception text."""
        provider = getattr(error, "provider", "")
        if not provider and task.task_type == "SEARCH":
            provider = getattr(self.search, "provider_name", "")
        diagnostic = {
            "provider": provider,
            "error_type": type(error).__name__,
            "native_error_type": getattr(error, "native_error_type", ""),
            "http_status": getattr(error, "http_status", None),
            "retry": retry,
        }
        task.result_summary.setdefault("failure_diagnostics", []).append(diagnostic)

    def _search_task(self, mission: Mission, task: ResearchTask) -> None:
        hits = self.search.search(task.payload["query"], num_results=10)
        self._count(mission, "candidates_discovered", len(hits))
        seen = set()
        for hit in hits:
            domain = store.normalize_domain(hit.url)
            if not domain or domain in seen: self._count(mission, "duplicates_skipped"); continue
            seen.add(domain)
            candidate = Candidate(name=hit.title or domain, source="mission_search", source_url=hit.url, query=task.payload["query"])
            store.save_candidate(self.db_path, candidate)
            profile_reason = self._search_profile_reason(mission, candidate)
            if profile_reason:
                candidate.campaign_status, candidate.campaign_reason = "out_of_profile", profile_reason
                candidate.target_fit_outcome, candidate.target_fit_reason = "OUT_OF_PROFILE", profile_reason
                store.save_candidate(self.db_path, candidate)
                self._count(mission, "out_of_profile")
                store.log_mission_event(self.db_path, mission.id, "CANDIDATE_OUT_OF_PROFILE", {"candidate_id": candidate.id, "reason": profile_reason})
                continue
            existing = store.find_entity_by_domain(self.db_path, domain)
            if existing:
                latest = store.latest_observation_for_entity(self.db_path, existing.id)
                if latest and not latest.get("error") and (latest.get("status_code") or 0) < 400 and self._fresh(latest.get("fetched_at", ""), mission.freshness_seconds):
                    self._count(mission, "duplicates_skipped"); self._count(mission, "recent_entities_skipped"); continue
                store.save_candidate(self.db_path, candidate); store.link_candidate_to_entity(self.db_path, candidate, existing.id)
                store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=existing.id, parent_task_id=task.id, task_type="REVISIT_ENTITY", priority=58, payload={"url": hit.url, "candidate_id": candidate.id}))
                self._count(mission, "stale_entities_revisited"); continue
            if mission.counters.get("unique_entities", 0) >= mission.max_entities:
                break
            candidate.campaign_status = "eligible"
            store.save_candidate(self.db_path, candidate)
            entity = store.resolve_entity(self.db_path, candidate.name, hit.url, mission.geographic_scope, industry=task.payload["industry"])
            store.link_candidate_to_entity(self.db_path, candidate, entity.id)
            store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=entity.id, parent_task_id=task.id,
                                                        task_type="INVESTIGATE_ENTITY", priority=60, payload={"url": hit.url, "candidate_id": candidate.id}))
            self._count(mission, "unique_entities"); store.log_mission_event(self.db_path, mission.id, "ENTITY_DISCOVERED", {"entity_id": entity.id})
        task.result_summary = {"hits": len(hits)}; store.log_mission_event(self.db_path, mission.id, "SEARCH_COMPLETED", task.result_summary)

    @staticmethod
    def _search_profile_reason(mission: Mission, candidate: Candidate) -> str:
        """Only campaign-local fit decisions; no business is globally excluded."""
        if mission.target_profile == "unknown":
            return "insufficient public evidence to select a campaign profile"
        if mission.target_profile != "local_sme":
            return ""
        identity = f"{candidate.name} {store.normalize_domain(candidate.source_url)}".lower()
        # These are explicit public identity signals, not a size inference from a weak website.
        signals = ("carrefour", " franchise", " retail chain", " multinational")
        return "confirmed enterprise/chain identity signal for local_sme campaign" if any(signal in identity for signal in signals) else ""

    @staticmethod
    def _has_enterprise_signal(candidate: Candidate, text: str) -> bool:
        """Use explicit public identity language, never a size guess from weak data."""
        identity = f"{candidate.name} {store.normalize_domain(candidate.source_url)} {text}".lower()
        # Kept intentionally short and visible: these are explicit chain/enterprise
        # identity claims, not a hidden brand-size classifier.
        return any(signal in identity for signal in (
            "carrefour", " franchise", "retail chain", "multinational",
            "part of the", "national chain",
        ))

    @staticmethod
    def _local_business_evidence(mission: Mission, metadata: list[dict], text: str) -> tuple[bool, bool]:
        """Return observed local location and direct Kenyan-contact evidence only."""
        scope = (mission.geographic_scope or "").strip().lower()
        location_observed = bool(scope and scope in text)
        if not location_observed:
            location_observed = any(place in text for place in (
                "nairobi", "mombasa", "kisumu", "nakuru", "eldoret", "kenya",
            ))
        contacts = [str(number) for meta in metadata for number in meta.get("phone_numbers", [])]
        emails = [str(email).lower() for meta in metadata for email in meta.get("emails", [])]
        kenyan_phone = any(re.search(r"(?:\+?254|0)7\d{8}\b", number.replace(" ", "")) for number in contacts)
        kenyan_email = any(email.endswith(".ke") for email in emails)
        return location_observed, kenyan_phone or kenyan_email

    def _assess_target_fit(self, mission: Mission, candidate: Candidate, metadata: list[dict], text: str,
                           contact_observed: bool, substantive: list[OpportunityFinding],
                           operations_need: bool) -> tuple[str, str]:
        """Make a bounded campaign-fit decision without asserting revenue or size."""
        profile = getattr(mission, "target_profile", "") or ""
        if not profile:
            return "NEEDS_HUMAN_REVIEW", "no target profile was set; legacy mission qualification is unchanged"
        if profile == "unknown":
            return "NEEDS_HUMAN_REVIEW", "target profile is unknown; public evidence cannot support strong ranking"
        if profile == "local_sme":
            if self._has_enterprise_signal(candidate, text):
                return "OUT_OF_PROFILE", "explicit public enterprise or chain signal for a local-SME campaign"
            if not contact_observed:
                return "NEEDS_HUMAN_REVIEW", "no public business contact channel observed"
            if not substantive:
                return "NEEDS_HUMAN_REVIEW", "no specific observable digital gap beyond SEO metadata was observed"
            location_observed, kenyan_contact = self._local_business_evidence(mission, metadata, text)
            if not location_observed or not kenyan_contact:
                return "NEEDS_HUMAN_REVIEW", "public evidence does not yet show both a local location and direct Kenyan business contact"
            return "LIKELY_LOCAL_SME", "public local location, direct Kenyan contact, and actionable digital gap observed; no enterprise signal observed"
        # Corporate operations has a different bar: public operational need,
        # contact, and an actionable gap. It does not infer company size.
        if not contact_observed:
            return "NEEDS_HUMAN_REVIEW", "no public business contact channel observed"
        if not substantive:
            return "NEEDS_HUMAN_REVIEW", "no specific observable digital gap beyond SEO metadata was observed"
        if not operations_need:
            return "NEEDS_HUMAN_REVIEW", "no public operations-system need was observed"
        return "NEEDS_HUMAN_REVIEW", "public operations-system need, contact, and actionable gap observed"

    def _investigate_task(self, mission: Mission, task: ResearchTask) -> None:
        entity = store.get_entity(self.db_path, task.entity_id)
        url = task.payload["url"]
        pages = prioritise_pages(url, task.payload.get("links", []), mission.max_pages_per_entity)
        for page in pages:
            store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=entity.id, parent_task_id=task.id,
                                                        task_type="FETCH_PAGE", priority=70 if page == url else 65,
                                                        max_attempts=mission.max_retries + 1, payload={"url": page, "candidate_id": task.payload["candidate_id"]}))
        self._count(mission, "entities_investigated"); task.result_summary = {"scheduled_pages": len(pages)}
        store.log_mission_event(self.db_path, mission.id, "INVESTIGATION_STARTED", {"entity_id": entity.id})

    def _fetch_task(self, mission: Mission, task: ResearchTask) -> None:
        url = task.payload["url"]; self.politeness.wait(url)
        result = self.browser.fetch(url)
        snapshot_meta = result.html_meta or {}
        observation = SourceObservation(candidate_id=task.payload["candidate_id"], source_url=result.url, fetched_at=_now(),
                                        status_code=result.status_code, title=result.title, evidence_excerpt=result.text_content[:500],
                                        html_meta=snapshot_meta, error=result.error)
        observation.facts = [SourceFact("title", result.title or "", result.url, (result.title or "")[:200], confidence="observed")]
        store.save_source_observation(self.db_path, observation); self._count(mission, "pages_fetched")
        if result.error:
            if result.error.startswith("ROBOTS_"):
                store.log_mission_event(self.db_path, mission.id, "PAGE_SKIPPED_POLICY", {"url": url, "policy": result.error})
                raise PolicySkipped(result.error)
            self._count(mission, "fetch_failures")
            raise RuntimeError(result.error)
        if result.status_code and result.status_code >= 400:
            self._count(mission, "fetch_failures"); raise HTTPTaskError(result.status_code)
        links = snapshot_meta.get("links", [])
        if links and task.parent_task_id:
            parent = next((item for item in store.tasks_for_mission(self.db_path, mission.id) if item.id == task.parent_task_id), None)
            if parent:
                current = [item for item in store.tasks_for_mission(self.db_path, mission.id) if item.entity_id == task.entity_id and item.task_type == "FETCH_PAGE"]
                for page in prioritise_pages(url, links, mission.max_pages_per_entity):
                    if len(current) >= mission.max_pages_per_entity:
                        break
                    if any(item.payload.get("url") == page for item in current):
                        continue
                    followup = ResearchTask(mission_id=mission.id, entity_id=task.entity_id, parent_task_id=task.id, task_type="FETCH_PAGE", priority=64, payload={"url": page, "candidate_id": task.payload["candidate_id"]})
                    store.save_task(self.db_path, followup); current.append(followup); store.log_mission_event(self.db_path, mission.id, "FOLLOWUP_CREATED", {"url": page})
        task.result_summary = {"url": result.url, "status_code": result.status_code}
        store.log_mission_event(self.db_path, mission.id, "PAGE_FETCHED", task.result_summary)
        store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=task.entity_id, parent_task_id=task.id,
                                                    task_type="EVALUATE_ENTITY", priority=30, payload={"candidate_id": task.payload["candidate_id"]}))

    def _evaluate_task(self, mission: Mission, task: ResearchTask) -> None:
        """Only emit findings when multi-page coverage makes absence meaningful."""
        candidate_id = task.payload["candidate_id"]
        observations = store.latest_source_observations(self.db_path, candidate_id)
        usable = [item for item in observations if not item.get("error") and (item.get("status_code") or 0) < 400]
        if not usable:
            task.result_summary = {"state": "insufficient_evidence"}; return
        entity = store.get_entity(self.db_path, task.entity_id)
        text = " ".join(item.get("evidence_excerpt", "") for item in usable).lower()
        metadata = [item.get("html_meta", {}) for item in usable]
        coverage = len(usable) >= min(2, mission.max_pages_per_entity)
        findings: list[OpportunityFinding] = []
        evidence = usable[0]
        def add(category, observed, consequence, opportunity, capability, severity="medium", tags=None, pattern="", reference=""):
            pattern = pattern if pattern and brain.pattern_exists(pattern) else ""
            capability = capability if brain.capability_exists(capability) else ""
            if pattern and capability and not brain.pattern_includes_capability(pattern, capability): capability = ""
            project = brain.project_by_identifier(reference) if reference else None
            if project and (not pattern or not brain.project_supports_pattern(project.id, pattern)): project = None
            findings.append(OpportunityFinding(category=category, observed=observed, evidence_url=evidence["source_url"], evidence_excerpt=evidence.get("evidence_excerpt", "")[:240], business_consequence=consequence, opportunity=opportunity, problem_tags=tags or [], solution_pattern=pattern, alcatrax_capability=capability, reference_project=project.client_name if project else "", severity=severity, confidence="observed"))
        product_signal = any(token in text for token in ("product", "shop", "price", "catalogue", "stock"))
        ecommerce = any(meta.get("has_ecommerce_words") for meta in metadata)
        catalogue = ecommerce or any("shop" in item.get("source_url", "").lower() or "catalog" in item.get("source_url", "").lower() for item in usable)
        if coverage and product_signal and not catalogue:
            if entity.industry == "automotive":
                projects = brain.projects_for_pattern("automotive_digital_catalogue")
                add("possible_catalogue_opportunity", "Products/services are visible across checked pages but no catalogue or shop flow was observed.", "Product discovery may require manual contact.", "Consider a searchable digital catalogue.", "catalogue", "high", ["no_catalogue", "poor_product_discovery"], "automotive_digital_catalogue", projects[0].client_name if projects else "")
            else:
                add("possible_ecommerce_opportunity", "Product-selling signals were observed across checked pages but no cart, checkout, catalogue, or shop flow was observed.", "Online product discovery may remain manual.", "Consider a catalogue or ecommerce discovery flow.", "ecommerce", "medium")
        service_business = entity.industry in ("hospitality", "health_fitness", "education_professional", "construction_services")
        booking = any(any(word in str(meta).lower() for word in ("booking", "reservation", "appointment")) for meta in metadata)
        if coverage and service_business and any(word in text for word in ("service", "appointment", "restaurant", "salon", "clinic", "hotel")) and not booking:
            add("possible_booking_opportunity", "Service-oriented pages were checked without an observed booking or reservation flow.", "Customers may need to call or message to arrange service.", "Consider a bounded booking or reservation flow.", "booking", "medium")
        if any(not meta.get("meta_description") or not meta.get("has_json_ld") for meta in metadata):
            add("possible_seo_structure_opportunity", "A checked first-party page lacks metadata or structured-data signals.", "Search presentation and machine-readable business context may be incomplete.", "Consider an SEO and structured-data foundation pass.", "seo", "low")
        candidate = Candidate(**next(item for item in store.candidates_for_entity(self.db_path, task.entity_id) if item["id"] == candidate_id))
        profile = BusinessProfile(candidate_id=candidate.id, business_name=entity.canonical_name, what_they_sell="", target_customers="", location=entity.location, contact_channels=[], has_ecommerce=ecommerce, extracted_from=[item["source_url"] for item in usable], confidence_note="Deterministic mission reconnaissance.")
        verification = VerificationResult(candidate_id=candidate.id, sources_checked=profile.extracted_from)
        contact_observed = any(meta.get("phone_numbers") or meta.get("emails") or meta.get("has_whatsapp_link") for meta in metadata)
        substantive = [finding for finding in findings if finding.category != "possible_seo_structure_opportunity"]
        operations_terms = ("inventory", "stock", "workflow", "report", "dashboard", "booking", "appointment")
        operations_need = any(term in text for term in operations_terms)
        outcome, reason = self._assess_target_fit(mission, candidate, metadata, text, contact_observed, substantive, operations_need)
        candidate.target_fit_outcome, candidate.target_fit_reason = outcome, reason
        candidate.campaign_reason = reason
        candidate.campaign_status = "out_of_profile" if outcome == "OUT_OF_PROFILE" else "eligible"
        store.save_candidate(self.db_path, candidate)
        profile_name = getattr(mission, "target_profile", "") or ""
        qualifies = (
            not profile_name or
            (profile_name == "local_sme" and outcome == "LIKELY_LOCAL_SME") or
            (profile_name == "corporate_operations" and contact_observed and bool(substantive) and operations_need)
        )
        if not qualifies:
            brief = brief_mod.build_brief(candidate, profile, verification, [])
            brief.status = "needs_human_review" if outcome == "NEEDS_HUMAN_REVIEW" else "out_of_profile"
            store.save_brief(self.db_path, brief)
            self._count(mission, "out_of_profile" if outcome == "OUT_OF_PROFILE" else "needs_human_review")
            task.result_summary = {"state": "out_of_profile", "target_fit_outcome": outcome, "reason": reason}
            store.log_mission_event(self.db_path, mission.id, "ENTITY_OUT_OF_PROFILE" if outcome == "OUT_OF_PROFILE" else "ENTITY_NEEDS_HUMAN_REVIEW", {"entity_id": entity.id, "reason": reason})
            return
        findings = substantive
        brief = brief_mod.build_brief(candidate, profile, verification, findings); store.save_brief(self.db_path, brief)
        self._count(mission, "opportunities_found", len(findings)); task.result_summary = {
            "state": "evaluated", "findings": len(findings),
            "target_fit_outcome": outcome, "reason": reason,
        }
        store.log_mission_event(self.db_path, mission.id, "ENTITY_EVALUATED", task.result_summary)

    def build_report(self, mission_id: str) -> str:
        mission = self._mission(mission_id); tasks = store.tasks_for_mission(self.db_path, mission_id)
        failed = [task for task in tasks if task.status == "FAILED"]
        c = defaultdict(int, mission.counters)
        candidate_ids = {task.payload.get("candidate_id") for task in tasks if task.payload.get("candidate_id")}
        entity_ids = list({task.entity_id for task in tasks if task.entity_id})
        ranked = [item for item in intelligence.rank_opportunities(self.db_path, entity_ids)
                  if item["candidate_id"] in candidate_ids]
        strong, strong_candidate_ids = [], set()
        for item in ranked:
            candidate_data = store.load_dossier(self.db_path, item["candidate_id"]).get("candidate") or {}
            profile_name = getattr(mission, "target_profile", "") or ""
            if profile_name == "local_sme" and candidate_data.get("target_fit_outcome") != "LIKELY_LOCAL_SME":
                continue
            if profile_name == "unknown":
                continue
            entity = store.get_entity(self.db_path, item["entity_id"])
            capacity, factors = self._capacity(item["entity_id"])
            op = item["opportunity"]
            strong_candidate_ids.add(item["candidate_id"])
            strong.append(f"### {item['business_name']}\n- **Target fit:** {candidate_data.get('target_fit_outcome', 'NEEDS_HUMAN_REVIEW')} — {candidate_data.get('target_fit_reason', 'not assessed')}\n- **Possible opportunity:** {op['opportunity']}\n- **Observed:** {op['observed']}\n- **Evidence:** {op['evidence_url']} — {op.get('evidence_excerpt', '')}\n- **Confidence / priority:** {op['confidence']} / {item['priority_score']}\n- **Solution:** {op.get('solution_pattern') or 'no pattern asserted'} / {op.get('alcatrax_capability') or 'none'}\n- **Past work:** {op.get('reference_project') or 'none asserted'}\n- **Public Business Readiness:** {capacity} ({'; '.join(factors) or 'insufficient public evidence'})\n- **Freshness:** {'recent' if entity and store.latest_observation_for_entity(self.db_path, entity.id) else 'unknown'}")
        insufficient = [task for task in tasks if task.task_type == "EVALUATE_ENTITY" and task.result_summary.get("state") == "insufficient_evidence"]
        needs_review = []
        for candidate_id in sorted(candidate_ids):
            candidate_data = store.load_dossier(self.db_path, candidate_id).get("candidate") or {}
            if candidate_id not in strong_candidate_ids:
                needs_review.append(f"- {candidate_data.get('name', candidate_id)}: **{candidate_data.get('target_fit_outcome', 'NEEDS_HUMAN_REVIEW')}** — {candidate_data.get('target_fit_reason', 'target fit was not assessed')}")
        all_observations = [item for candidate_id in candidate_ids for item in store.get_source_observations(self.db_path, candidate_id)]
        latest_observations = [item for candidate_id in candidate_ids for item in store.latest_source_observations(self.db_path, candidate_id)]
        limits = f"Workers: {mission.worker_count} (bounded 1–8)\\n\\nRetries per task: {mission.max_retries}\\n\\nFreshness window: {mission.freshness_seconds} seconds\\n\\nTime budget: {mission.time_budget_seconds or 'none'} seconds\\n\\nSearch limit: {mission.max_searches}\\n\\nEntity limit: {mission.max_entities}\\n\\nPages per entity: {mission.max_pages_per_entity}\\n\\nTotal page limit: {mission.max_total_pages}\\n\\nTarget profile: {mission.target_profile}"
        def failure_line(task: ResearchTask) -> str:
            diagnostics = task.result_summary.get("failure_diagnostics", [])
            if not diagnostics:
                return f"- {task.task_type}: {task.error}"
            details = "; ".join(
                f"provider={item['provider'] or 'unknown'}, error_type={item['error_type']}, "
                f"native_error_type={item['native_error_type'] or 'none'}, "
                f"http_status={item['http_status'] if item['http_status'] is not None else 'none'}, "
                f"retry={'yes' if item['retry'] else 'no'}"
                for item in diagnostics
            )
            return f"- {task.task_type}: {task.error} [{details}]"

        report = f"# Scout Mission Report\n\n## Mission\n\nObjective: {mission.objective}\n\nScope: {mission.geographic_scope}\n\nStatus: {mission.status}\n\nStop reason: {mission.stop_reason}\n\n## Effective Safety Limits\n\n{limits}\n\n## Activity\n\nSearches: {c['searches_executed']}\n\nBusinesses discovered: {c['candidates_discovered']}\n\nUnique entities: {c['unique_entities']}\n\nDuplicates: {c['duplicates_skipped']}\n\nEntities investigated: {c['entities_investigated']}\n\nPages fetched: {c['pages_fetched']}\n\nFailures: {c['failures']}\n\n## Evidence History\n\nLatest source observations: {len(latest_observations)}\n\nPrevious observation versions retained: {len(all_observations) - len(latest_observations)}\n\nAssessments use the latest observation per URL; earlier versions remain in SQLite for audit.\n\n## Market Coverage\n\nIndustries: {', '.join(mission.industries)}\n\nBusiness types: {', '.join(mission.business_types)}\n\n## Strong Evidence-Backed Opportunities\n\n" + ("\n\n".join(strong) if strong else "None identified from available evidence.") + "\n\n## Needs Human Review\n\n" + ("\n".join(needs_review) if needs_review else "None.") + "\n\n## Insufficient Evidence\n\n" + ("\n".join(f"- Entity task {task.entity_id}: coverage/fetch evidence was insufficient." for task in insufficient) if insufficient else "None.") + "\n\n## Failures\n\n" + ("\n".join(failure_line(task) for task in failed) if failed else "None.") + "\n\n## Mission Summary\n\nThis report contains public, deterministic evidence only. Target-fit outcomes describe only the observed public signals and do not prove ability to pay, revenue, owner status, or company size. Public Business Readiness is a public-web sales-prioritisation signal, not proof of revenue, creditworthiness, or financial health."
        store.save_mission_report(self.db_path, mission_id, report); return report

    def _capacity(self, entity_id: str) -> tuple[str, list[str]]:
        observations = store.observations_for_entity(self.db_path, entity_id)
        if not observations:
            return "INSUFFICIENT EVIDENCE", []
        text = " ".join(item.get("evidence_excerpt", "") for item in observations).lower(); metas = [item.get("html_meta", {}) for item in observations]
        factors = []
        if any(word in text for word in ("established in", "since 19", "since 20")): factors.append("public longevity claim")
        if any(word in text for word in ("branches", "locations", "our nairobi and")): factors.append("multi-location language")
        entity = store.get_entity(self.db_path, entity_id)
        if entity and entity.primary_domain and not any(host in entity.primary_domain for host in ("facebook.com", "instagram.com", "wixsite.com", "blogspot.")): factors.append("dedicated-domain web investment")
        if any(meta.get("has_ecommerce_words") for meta in metas): factors.append("observed ecommerce infrastructure")
        if sum(bool(meta.get(key)) for meta in metas for key in ("phone_numbers", "emails", "has_whatsapp_link")) >= 2: factors.append("contact infrastructure")
        return ("STRONG" if len(factors) >= 3 else "MODERATE" if len(factors) >= 2 else "WEAK" if factors else "INSUFFICIENT EVIDENCE"), factors

    def _count(self, mission: Mission, name: str, increment: int = 1) -> None:
        with self._persist_lock:
            mission.counters[name] = mission.counters.get(name, 0) + increment; store.save_mission(self.db_path, mission)

    @staticmethod
    def _fresh(value: str, freshness_seconds: int) -> bool:
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds() < freshness_seconds
        except (TypeError, ValueError):
            return False

    def _limit_reached(self, mission: Mission, task: ResearchTask) -> bool:
        c = mission.counters
        return (task.task_type == "SEARCH" and not task.attempts and c.get("searches_executed", 0) >= mission.max_searches) or (task.task_type == "INVESTIGATE_ENTITY" and c.get("unique_entities", 0) > mission.max_entities) or (task.task_type == "FETCH_PAGE" and c.get("pages_fetched", 0) >= mission.max_total_pages)

    @staticmethod
    def _validate_limits(limits: dict) -> None:
        ranges = {
            "worker_count": (1, 8), "max_retries": (0, 5), "max_entities": (1, 100),
            "max_searches": (1, 50), "max_pages_per_entity": (1, 10), "max_total_pages": (1, 500),
            "freshness_seconds": (0, 60 * 60 * 24 * 365),
        }
        for name, (minimum, maximum) in ranges.items():
            value = limits.get(name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum):
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        time_budget = limits.get("time_budget_seconds")
        if time_budget is not None and (not isinstance(time_budget, int) or isinstance(time_budget, bool) or not 1 <= time_budget <= 60 * 60 * 24):
            raise ValueError("time_budget_seconds must be an integer between 1 and 86400")

    def _finish(self, mission: Mission, status: str, reason: str) -> None:
        mission.status, mission.stop_reason, mission.completed_at = status, reason, _now(); store.save_mission(self.db_path, mission)
        store.log_mission_event(self.db_path, mission.id, "MISSION_COMPLETED" if status == "COMPLETED" else "MISSION_STOPPED", {"reason": reason})

    def _mission(self, mission_id: str) -> Mission:
        mission = store.get_mission(self.db_path, mission_id)
        if not mission: raise ValueError(f"Unknown mission: {mission_id}")
        return mission
