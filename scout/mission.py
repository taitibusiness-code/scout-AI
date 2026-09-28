"""Offline-first autonomous research loop. It only performs public research."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic, sleep
from urllib.parse import urlparse, urljoin
import random
import threading

from . import store
from .models import Candidate, Mission, ResearchTask, SourceFact, SourceObservation, BusinessProfile, VerificationResult, OpportunityFinding
from . import alcatrax_knowledge as brain, brief as brief_mod, intelligence
from .taxonomy import business_types_for, normalize_industries


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_queries(mission: Mission, completed_queries: set[str] | None = None) -> list[tuple[str, str]]:
    """Generate bounded, non-duplicated search angles without any LLM."""
    completed_queries = completed_queries or set()
    rows = []
    for industry in normalize_industries(mission.industries):
        for business_type in business_types_for([industry]):
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

    def create(self, objective: str, location: str = "", industries: list[str] | None = None, **limits) -> Mission:
        selected = normalize_industries(industries)
        self._validate_limits(limits)
        mission = Mission(objective=objective, geographic_scope=location, industries=selected,
                          business_types=business_types_for(selected), **limits)
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
        try:
            if task.task_type == "SEARCH": self._search_task(mission, task)
            elif task.task_type == "FETCH_PAGE": self._fetch_task(mission, task)
            elif task.task_type in ("INVESTIGATE_ENTITY", "REVISIT_ENTITY"): self._investigate_task(mission, task)
            elif task.task_type == "EVALUATE_ENTITY": self._evaluate_task(mission, task)
            else: task.result_summary = {"skipped": "not needed in deterministic pass"}
            task.status, task.completed_at, task.error = "COMPLETED", _now(), ""
        except HTTPTaskError as error:
            task.error = str(error)
            if error.transient and task.attempts < task.max_attempts:
                task.status, task.priority = "PENDING", task.priority - 1
                sleep(min(0.05 * (2 ** (task.attempts - 1)) + random.random() * 0.01, 0.2))
            else:
                task.status, task.completed_at = "FAILED", _now(); self._count(mission, "failures")
        except Exception as error:
            task.error = str(error)
            if task.attempts < task.max_attempts:
                task.status = "PENDING"; task.priority -= 1
                sleep(min(0.05 * (2 ** (task.attempts - 1)) + random.random() * 0.01, 0.2))
            else:
                task.status, task.completed_at = "FAILED", _now()
                self._count(mission, "failures")
        store.save_task(self.db_path, task)

    def _search_task(self, mission: Mission, task: ResearchTask) -> None:
        hits = self.search.search(task.payload["query"], num_results=10)
        self._count(mission, "searches_executed"); self._count(mission, "candidates_discovered", len(hits))
        seen = set()
        for hit in hits:
            domain = store.normalize_domain(hit.url)
            if not domain or domain in seen: self._count(mission, "duplicates_skipped"); continue
            seen.add(domain)
            existing = store.find_entity_by_domain(self.db_path, domain)
            if existing:
                latest = store.latest_observation_for_entity(self.db_path, existing.id)
                if latest and not latest.get("error") and (latest.get("status_code") or 0) < 400 and self._fresh(latest.get("fetched_at", ""), mission.freshness_seconds):
                    self._count(mission, "duplicates_skipped"); self._count(mission, "recent_entities_skipped"); continue
                candidate = Candidate(name=hit.title or domain, source="mission_search", source_url=hit.url, query=task.payload["query"])
                store.save_candidate(self.db_path, candidate); store.link_candidate_to_entity(self.db_path, candidate, existing.id)
                store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=existing.id, parent_task_id=task.id, task_type="REVISIT_ENTITY", priority=58, payload={"url": hit.url, "candidate_id": candidate.id}))
                self._count(mission, "stale_entities_revisited"); continue
            if mission.counters.get("unique_entities", 0) >= mission.max_entities:
                break
            candidate = Candidate(name=hit.title or domain, source="mission_search", source_url=hit.url, query=task.payload["query"])
            store.save_candidate(self.db_path, candidate)
            entity = store.resolve_entity(self.db_path, candidate.name, hit.url, mission.geographic_scope, industry=task.payload["industry"])
            store.link_candidate_to_entity(self.db_path, candidate, entity.id)
            store.save_task(self.db_path, ResearchTask(mission_id=mission.id, entity_id=entity.id, parent_task_id=task.id,
                                                        task_type="INVESTIGATE_ENTITY", priority=60, payload={"url": hit.url, "candidate_id": candidate.id}))
            self._count(mission, "unique_entities"); store.log_mission_event(self.db_path, mission.id, "ENTITY_DISCOVERED", {"entity_id": entity.id})
        task.result_summary = {"hits": len(hits)}; store.log_mission_event(self.db_path, mission.id, "SEARCH_COMPLETED", task.result_summary)

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
            self._count(mission, "fetch_failures")
            if result.error.startswith("ROBOTS_"):
                store.log_mission_event(self.db_path, mission.id, "PAGE_SKIPPED_POLICY", {"url": url, "policy": result.error})
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
        brief = brief_mod.build_brief(candidate, profile, verification, findings); store.save_brief(self.db_path, brief)
        self._count(mission, "opportunities_found", len(findings)); task.result_summary = {"state": "evaluated", "findings": len(findings)}
        store.log_mission_event(self.db_path, mission.id, "ENTITY_EVALUATED", task.result_summary)

    def build_report(self, mission_id: str) -> str:
        mission = self._mission(mission_id); tasks = store.tasks_for_mission(self.db_path, mission_id)
        failed = [task for task in tasks if task.status == "FAILED"]
        c = defaultdict(int, mission.counters)
        ranked = intelligence.rank_opportunities(self.db_path)
        strong = []
        for item in ranked:
            entity = store.get_entity(self.db_path, item["entity_id"])
            capacity, factors = self._capacity(item["entity_id"])
            op = item["opportunity"]
            strong.append(f"### {item['business_name']}\n- **Possible opportunity:** {op['opportunity']}\n- **Observed:** {op['observed']}\n- **Evidence:** {op['evidence_url']} — {op.get('evidence_excerpt', '')}\n- **Confidence / priority:** {op['confidence']} / {item['priority_score']}\n- **Solution:** {op.get('solution_pattern') or 'no pattern asserted'} / {op.get('alcatrax_capability') or 'none'}\n- **Past work:** {op.get('reference_project') or 'none asserted'}\n- **Public Business Readiness:** {capacity} ({'; '.join(factors) or 'insufficient public evidence'})\n- **Freshness:** {'recent' if entity and store.latest_observation_for_entity(self.db_path, entity.id) else 'unknown'}")
        insufficient = [task for task in tasks if task.task_type == "EVALUATE_ENTITY" and task.result_summary.get("state") == "insufficient_evidence"]
        candidate_ids = {task.payload.get("candidate_id") for task in tasks if task.payload.get("candidate_id")}
        all_observations = [item for candidate_id in candidate_ids for item in store.get_source_observations(self.db_path, candidate_id)]
        latest_observations = [item for candidate_id in candidate_ids for item in store.latest_source_observations(self.db_path, candidate_id)]
        limits = f"Workers: {mission.worker_count} (bounded 1–8)\\n\\nRetries per task: {mission.max_retries}\\n\\nFreshness window: {mission.freshness_seconds} seconds\\n\\nTime budget: {mission.time_budget_seconds or 'none'} seconds\\n\\nSearch limit: {mission.max_searches}\\n\\nEntity limit: {mission.max_entities}\\n\\nPages per entity: {mission.max_pages_per_entity}\\n\\nTotal page limit: {mission.max_total_pages}"
        report = f"# Scout Mission Report\n\n## Mission\n\nObjective: {mission.objective}\n\nScope: {mission.geographic_scope}\n\nStatus: {mission.status}\n\nStop reason: {mission.stop_reason}\n\n## Effective Safety Limits\n\n{limits}\n\n## Activity\n\nSearches: {c['searches_executed']}\n\nBusinesses discovered: {c['candidates_discovered']}\n\nUnique entities: {c['unique_entities']}\n\nDuplicates: {c['duplicates_skipped']}\n\nEntities investigated: {c['entities_investigated']}\n\nPages fetched: {c['pages_fetched']}\n\nFailures: {c['failures']}\n\n## Evidence History\n\nLatest source observations: {len(latest_observations)}\n\nPrevious observation versions retained: {len(all_observations) - len(latest_observations)}\n\nAssessments use the latest observation per URL; earlier versions remain in SQLite for audit.\n\n## Market Coverage\n\nIndustries: {', '.join(mission.industries)}\n\nBusiness types: {', '.join(mission.business_types)}\n\n## Strong Evidence-Backed Opportunities\n\n" + ("\n\n".join(strong) if strong else "None identified from available evidence.") + "\n\n## Worth Deeper Investigation\n\n" + ("Entities with single-page or ambiguous coverage remain unranked." if not strong else "See insufficient-evidence items below where coverage was incomplete.") + "\n\n## Insufficient Evidence\n\n" + ("\n".join(f"- Entity task {task.entity_id}: coverage/fetch evidence was insufficient." for task in insufficient) if insufficient else "None.") + "\n\n## Failures\n\n" + ("\n".join(f"- {task.task_type}: {task.error}" for task in failed) if failed else "None.") + "\n\n## Mission Summary\n\nThis report contains public, deterministic evidence only. Public Business Readiness is a public-web sales-prioritisation signal, not proof of revenue, creditworthiness, or financial health."
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
        return (task.task_type == "SEARCH" and c.get("searches_executed", 0) >= mission.max_searches) or (task.task_type == "INVESTIGATE_ENTITY" and c.get("unique_entities", 0) > mission.max_entities) or (task.task_type == "FETCH_PAGE" and c.get("pages_fetched", 0) >= mission.max_total_pages)

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
