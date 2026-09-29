import tempfile
import threading
import time
import json
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path

from scout import store
from scout.mission import DomainPoliteness, ScoutMissionEngine, plan_queries, prioritise_pages
from scout.models import Mission
from scout.providers.base import BrowserProvider, FetchResult, SearchHit, SearchProvider
from scout.providers.exa_search import ExaSearchProvider
from scout.providers.search_errors import SearchRequestError, TransientSearchError


class FakeSearch(SearchProvider):
    def __init__(self, hits=None): self.hits = hits or [SearchHit("Acme Hardware", "https://acme.test", "tools")]
    def search(self, query, num_results=10): return self.hits


class FailingSearch(SearchProvider):
    provider_name = "exa"

    def __init__(self, error): self.error, self.calls = error, 0
    def search(self, query, num_results=10):
        self.calls += 1
        raise self.error


class FakePlaces(SearchProvider):
    provider_name = "google_places"
    def __init__(self, website="", details_error=None):
        self.website, self.details_error, self.search_calls, self.detail_calls = website, details_error, 0, 0
        self.place_id = "place-id-safe"
    def search(self, query, num_results=10):
        self.search_calls += 1
        return [SearchHit("MAPS_SENTINEL_NAME", self.website, "", {"provider": "google_places", "place_id": self.place_id,
            "maps_only": not bool(self.website), "places_identity": {"formattedAddress": "MAPS_SENTINEL_ADDRESS", "nationalPhoneNumber": "MAPS_SENTINEL_PHONE", "googleMapsUri": "MAPS_SENTINEL_URI"}})]
    def website_uri(self, place_id):
        self.detail_calls += 1
        if self.details_error: raise self.details_error
        return self.website


class PlacesOnlySearch(SearchProvider):
    provider_name = "combined_discovery"
    def __init__(self, places): self.providers = [places]
    def search(self, query, num_results=10): return self.providers[0].search(query, num_results)


class FirstPartyBrowser(BrowserProvider):
    def __init__(self): self.urls = []
    def fetch(self, url, timeout=15, max_chars=8000):
        self.urls.append(url)
        return FetchResult("https://verified.test/", 200, "FIRST_PARTY_TITLE", "Nairobi products and stock", {"phone_numbers": ["+254700000000"], "links": []})


class FakeBrowser(BrowserProvider):
    def __init__(self, fail=False): self.fail = fail; self.urls = []
    def fetch(self, url, timeout=15, max_chars=8000):
        self.urls.append(url)
        if self.fail: return FetchResult(url, None, None, "", {}, "offline")
        return FetchResult(url, 200, "Acme", "Hardware and services", {"links": ["/contact", "/shop", "/login"]})


class StatusBrowser(BrowserProvider):
    def __init__(self, status): self.status = status; self.calls = 0
    def fetch(self, url, timeout=15, max_chars=8000):
        self.calls += 1
        return FetchResult(url, self.status, "Acme", "products services", {"links": ["/contact"]})


class ContactGapBrowser(BrowserProvider):
    def fetch(self, url, timeout=15, max_chars=8000):
        return FetchResult(url, 200, "Acme", "Products and stock available", {
            "phone_numbers": ["+254700000000"], "emails": [], "has_whatsapp_link": False,
            "has_ecommerce_words": False, "meta_description": "", "has_json_ld": False,
        })


class MetadataOnlyBrowser(BrowserProvider):
    def fetch(self, url, timeout=15, max_chars=8000):
        return FetchResult(url, 200, "Acme", "Welcome to our business", {
            "phone_numbers": ["+254700000000"], "emails": [], "has_whatsapp_link": False,
            "has_ecommerce_words": False, "meta_description": "", "has_json_ld": False,
        })


class LocalBusinessGapBrowser(BrowserProvider):
    def fetch(self, url, timeout=15, max_chars=8000):
        return FetchResult(url, 200, "Acme", "Acme Hardware, Tom Mboya Street, Nairobi. Products and stock available.", {
            "phone_numbers": ["+254700000000"], "emails": [], "has_whatsapp_link": True,
            "has_ecommerce_words": False, "meta_description": "", "has_json_ld": True,
        })


class BlockingBrowser(BrowserProvider):
    """Blocks active fetches so the scheduler's actual overlap is observable."""
    def __init__(self, expected_parallel=2):
        self.expected_parallel = expected_parallel
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.by_host = {}
        self.host_max = {}

    def fetch(self, url, timeout=15, max_chars=8000):
        host = url.split("/")[2]
        with self.lock:
            self.active += 1; self.max_active = max(self.max_active, self.active)
            self.by_host[host] = self.by_host.get(host, 0) + 1
            self.host_max[host] = max(self.host_max.get(host, 0), self.by_host[host])
            if self.active >= self.expected_parallel: self.started.set()
        self.release.wait(3)
        with self.lock:
            self.active -= 1; self.by_host[host] -= 1
        return FetchResult(url, 200, "Acme", "services", {})


class MissionEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.db = str(Path(self.temp.name) / "scout.db")
    def tearDown(self): self.temp.cleanup()

    def test_creation_persistence_and_all_industry_planning(self):
        engine = ScoutMissionEngine(self.db)
        mission = engine.create("Find clients", "Nairobi")
        self.assertEqual(6, len(mission.industries)); self.assertEqual(mission.id, store.get_mission(self.db, mission.id).id)
        self.assertGreaterEqual(len(store.tasks_for_mission(self.db, mission.id)), 18)

    def test_industry_plan_and_duplicate_prevention(self):
        mission = Mission(geographic_scope="Nairobi", industries=["hospitality"])
        queries = plan_queries(mission, {"hotels Nairobi"})
        self.assertEqual([("hospitality", "restaurants Nairobi")], queries)

    def test_prioritised_same_domain_pages_and_limit(self):
        pages = prioritise_pages("https://acme.test", ["/blog", "/shop", "https://other.test/contact", "/contact", "/login"], 3)
        self.assertEqual(["https://acme.test", "https://acme.test/contact", "https://acme.test/shop"], pages)

    def test_offline_mission_runs_without_llm_and_creates_followups(self):
        browser = FakeBrowser(); engine = ScoutMissionEngine(self.db, FakeSearch(), browser)
        mission = engine.create("Find hardware", "Nairobi", ["retail_local"], max_searches=1, max_pages_per_entity=3)
        done = engine.run(mission.id)
        self.assertEqual("STOPPED", done.status)
        self.assertIn("mission limit", done.stop_reason)
        tasks = store.tasks_for_mission(self.db, mission.id)
        self.assertGreaterEqual(len([task for task in tasks if task.task_type == "FETCH_PAGE"]), 3)
        self.assertGreaterEqual(done.counters["pages_fetched"], 3)
        self.assertIn("# Scout Mission Report", store.get_mission_report(self.db, mission.id))

    def test_unclassified_page_failure_is_not_retried(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), FakeBrowser(fail=True))
        mission = engine.create("Find hardware", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
        done = engine.run(mission.id)
        failures = [task for task in store.tasks_for_mission(self.db, mission.id) if task.status == "FAILED"]
        self.assertTrue(failures); self.assertEqual(1, failures[0].attempts); self.assertIn(done.status, ("STOPPED", "COMPLETED"))

    def test_search_budget_counts_started_searches_including_failures(self):
        search = FailingSearch(SearchRequestError("Exa search request failed.", provider="Exa"))
        engine = ScoutMissionEngine(self.db, search, FakeBrowser())
        mission = engine.create("budget", "Nairobi", ["retail_local"], max_searches=2, max_retries=1)
        done = engine.run(mission.id)
        search_tasks = [task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "SEARCH" and task.attempts]
        self.assertEqual(2, search.calls)
        self.assertEqual(2, len(search_tasks))
        self.assertEqual(2, done.counters["searches_executed"])

    def test_non_transient_search_error_is_not_retried(self):
        search = FailingSearch(SearchRequestError("Exa search request failed.", provider="Exa"))
        engine = ScoutMissionEngine(self.db, search, FakeBrowser())
        mission = engine.create("non-transient", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
        engine.run(mission.id)
        task = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "SEARCH" and task.attempts)
        self.assertEqual(1, search.calls); self.assertEqual(1, task.attempts)
        self.assertEqual([False], [item["retry"] for item in task.result_summary["failure_diagnostics"]])

    def test_transient_search_error_retries_to_configured_limit(self):
        search = FailingSearch(TransientSearchError("Exa search timed out.", provider="Exa", native_error_type="Timeout"))
        engine = ScoutMissionEngine(self.db, search, FakeBrowser())
        mission = engine.create("transient", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
        engine.run(mission.id)
        task = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "SEARCH" and task.attempts)
        self.assertEqual(2, search.calls); self.assertEqual(2, task.attempts)
        self.assertEqual([True, False], [item["retry"] for item in task.result_summary["failure_diagnostics"]])

    def test_search_failure_diagnostics_are_safe_and_reported(self):
        import requests
        raw_native_text = "DO_NOT_PERSIST_this_native_error_text"
        engine = ScoutMissionEngine(self.db, ExaSearchProvider("not-a-real-key"), FakeBrowser())
        mission = engine.create("diagnostics", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
        with patch("scout.providers.exa_search.requests.post", side_effect=requests.RequestException(raw_native_text)):
            engine.run(mission.id)
        task = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "SEARCH" and task.attempts)
        diagnostic = task.result_summary["failure_diagnostics"][0]
        self.assertEqual({"provider", "error_type", "native_error_type", "http_status", "retry"}, set(diagnostic))
        self.assertEqual("Exa", diagnostic["provider"]); self.assertEqual("SearchRequestError", diagnostic["error_type"])
        self.assertEqual("RequestException", diagnostic["native_error_type"]); self.assertIsNone(diagnostic["http_status"])
        report = store.get_mission_report(self.db, mission.id)
        self.assertIn("native_error_type=RequestException", report); self.assertNotIn(raw_native_text, report)
        with sqlite3.connect(self.db) as conn:
            persisted = "\n".join(row[0] for row in conn.execute("SELECT data FROM research_tasks WHERE mission_id=?", (mission.id,)))
        self.assertNotIn(raw_native_text, persisted)

    def test_pause_and_report_need_no_providers(self):
        engine = ScoutMissionEngine(self.db); mission = engine.create("Research", "Nairobi", ["automotive"])
        self.assertEqual("PAUSED", engine.pause(mission.id).status)
        self.assertIn("Research", engine.build_report(mission.id))

    def test_domain_limiter_tracks_hosts_independently(self):
        limiter = DomainPoliteness(0)
        limiter.wait("https://one.test/a"); limiter.wait("https://two.test/a")
        self.assertIn("one.test", limiter._last); self.assertIn("two.test", limiter._last)

    def test_http_503_retries_but_404_does_not(self):
        for status, expected in ((503, 2), (404, 1)):
            browser = StatusBrowser(status); engine = ScoutMissionEngine(self.db, FakeSearch(), browser)
            mission = engine.create(f"status {status}", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
            engine.run(mission.id)
            self.assertEqual(expected, browser.calls)

    def test_recent_entity_skipped_and_stale_entity_revisited(self):
        browser = FakeBrowser(); engine = ScoutMissionEngine(self.db, FakeSearch(), browser)
        first = engine.create("first", "Nairobi", ["retail_local"], max_searches=1); engine.run(first.id)
        recent = engine.create("recent", "Nairobi", ["retail_local"], max_searches=1); finished = engine.run(recent.id)
        self.assertGreaterEqual(finished.counters.get("recent_entities_skipped", 0), 1)
        entity = store.list_entities(self.db)[0]
        observation = store.latest_observation_for_entity(self.db, entity.id); observation["fetched_at"] = "2000-01-01T00:00:00+00:00"
        # A freshness threshold of zero deterministically makes the known entity stale.
        stale = engine.create("stale", "Nairobi", ["retail_local"], max_searches=1, freshness_seconds=0); done = engine.run(stale.id)
        self.assertGreaterEqual(done.counters.get("stale_entities_revisited", 0), 1)

    def test_page_fetch_concurrency_is_bounded_and_domain_exclusive(self):
        browser = BlockingBrowser(); engine = ScoutMissionEngine(self.db, FakeSearch(), browser, DomainPoliteness(0))
        mission = engine.create("bounded", "Nairobi", ["retail_local"], worker_count=2, max_searches=1, max_total_pages=3)
        for task in store.tasks_for_mission(self.db, mission.id, "PENDING"):
            task.status = "SKIPPED"; store.save_task(self.db, task)
        entity = store.resolve_entity(self.db, "Acme", "https://same.test", "Nairobi", industry="retail_local")
        candidate = __import__("scout.models", fromlist=["Candidate"]).Candidate(name="Acme", source_url="https://same.test")
        store.save_candidate(self.db, candidate); store.link_candidate_to_entity(self.db, candidate, entity.id)
        for url in ("https://same.test/one", "https://same.test/two", "https://other.test/one"):
            store.save_task(self.db, __import__("scout.models", fromlist=["ResearchTask"]).ResearchTask(mission_id=mission.id, entity_id=entity.id, task_type="FETCH_PAGE", payload={"url": url, "candidate_id": candidate.id}))
        worker = threading.Thread(target=engine.run, args=(mission.id,)); worker.start()
        self.assertTrue(browser.started.wait(3), "different public domains should retain bounded concurrency")
        self.assertEqual(2, browser.max_active)
        self.assertEqual(1, browser.host_max["same.test"])
        browser.release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_limit_validation_and_report_discloses_effective_limits(self):
        engine = ScoutMissionEngine(self.db)
        with self.assertRaises(ValueError): engine.create("bad", worker_count=9)
        mission = engine.create("limits", "Nairobi", ["automotive"], worker_count=1, max_retries=0,
                                 freshness_seconds=0, time_budget_seconds=5, max_entities=1,
                                 max_searches=1, max_pages_per_entity=1, max_total_pages=1)
        report = engine.build_report(mission.id)
        self.assertIn("Workers: 1 (bounded 1–8)", report); self.assertIn("Time budget: 5 seconds", report)

    def test_campaign_profile_narrows_queries_and_excludes_chain_only_for_local_sme(self):
        hits = [SearchHit("Carrefour Kenya", "https://carrefour.example"), SearchHit("Acme Hardware", "https://acme.test")]
        engine = ScoutMissionEngine(self.db, FakeSearch(hits), ContactGapBrowser())
        mission = engine.create("hardware", "Nairobi", ["retail_local"], business_types=["hardware shops"],
                                target_profile="local_sme", max_searches=1, max_entities=1, max_pages_per_entity=1)
        self.assertEqual([("retail_local", "hardware shops Nairobi")], plan_queries(mission))
        done = engine.run(mission.id)
        self.assertEqual(1, done.counters["unique_entities"])
        self.assertGreaterEqual(done.counters["out_of_profile"], 1)
        self.assertEqual("Acme Hardware", store.list_entities(self.db)[0].canonical_name)
        self.assertIn("CANDIDATE_OUT_OF_PROFILE", [event["event"] for event in store.mission_events(self.db, mission.id)])

    def test_unknown_profile_never_admits_an_entity(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), ContactGapBrowser())
        mission = engine.create("unknown", "Nairobi", ["retail_local"], target_profile="unknown", max_searches=1)
        done = engine.run(mission.id)
        self.assertEqual(0, done.counters.get("unique_entities", 0))
        self.assertGreaterEqual(done.counters.get("out_of_profile", 0), 1)

    def test_corporate_operations_allows_chain_when_public_operations_need_exists(self):
        engine = ScoutMissionEngine(self.db, FakeSearch([SearchHit("Carrefour Kenya", "https://carrefour.example")]), ContactGapBrowser())
        mission = engine.create("operations", "Nairobi", ["retail_local"], target_profile="corporate_operations",
                                max_searches=1, max_entities=1, max_pages_per_entity=1)
        done = engine.run(mission.id)
        self.assertEqual(1, done.counters["unique_entities"])
        evaluation = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "EVALUATE_ENTITY")
        self.assertEqual("evaluated", evaluation.result_summary["state"])

    def test_places_search_uses_one_logical_budget_and_no_site_persists_only_id(self):
        places = FakePlaces(); engine = ScoutMissionEngine(self.db, PlacesOnlySearch(places), FakeBrowser())
        mission = engine.create("places", "Nairobi", ["retail_local"], max_searches=1, max_entities=2, max_total_pages=2)
        done = engine.run(mission.id)
        self.assertEqual(1, places.search_calls); self.assertEqual(1, done.counters["searches_executed"])
        self.assertEqual(0, done.counters.get("pages_fetched", 0)); self.assertEqual([], store.list_entities(self.db))
        with sqlite3.connect(self.db) as conn:
            persisted = "\n".join(str(value) for row in conn.execute("SELECT data FROM research_tasks") for value in row)
        self.assertIn("place-id-safe", persisted)
        for sentinel in ("MAPS_SENTINEL_NAME", "MAPS_SENTINEL_ADDRESS", "MAPS_SENTINEL_PHONE", "MAPS_SENTINEL_URI"):
            self.assertNotIn(sentinel, persisted)

    def test_places_details_failure_creates_no_candidate_or_listing_leak(self):
        places = FakePlaces("https://maps-sentinel.example", TransientSearchError("details", provider="Google Places"))
        engine = ScoutMissionEngine(self.db, PlacesOnlySearch(places), FakeBrowser())
        mission = engine.create("places", "Nairobi", ["retail_local"], max_searches=1, max_entities=2, max_total_pages=2, max_retries=0)
        engine.run(mission.id)
        self.assertEqual([], store.list_entities(self.db))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])

    def test_places_verification_uses_first_party_only_and_respects_page_entity_caps(self):
        places, browser = FakePlaces("https://maps-sentinel.example"), FirstPartyBrowser()
        engine = ScoutMissionEngine(self.db, PlacesOnlySearch(places), browser)
        mission = engine.create("places", "Nairobi", ["retail_local"], max_searches=1, max_entities=1, max_total_pages=1, max_pages_per_entity=1)
        done = engine.run(mission.id)
        self.assertEqual(1, done.counters["unique_entities"]); self.assertEqual(1, done.counters["pages_fetched"])
        candidate = store.candidates_for_entity(self.db, store.list_entities(self.db)[0].id)[0]
        self.assertEqual("FIRST_PARTY_TITLE", candidate["name"]); self.assertEqual("verified.test", store.list_entities(self.db)[0].primary_domain)
        with sqlite3.connect(self.db) as conn:
            persisted = "\n".join(str(value) for row in conn.execute("SELECT data FROM candidates UNION ALL SELECT data FROM research_tasks UNION ALL SELECT data FROM entities" ) for value in row)
        for sentinel in ("MAPS_SENTINEL_NAME", "MAPS_SENTINEL_ADDRESS", "MAPS_SENTINEL_PHONE", "MAPS_SENTINEL_URI", "maps-sentinel.example"):
            self.assertNotIn(sentinel, persisted)

    def test_metadata_only_gap_is_out_of_profile(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), MetadataOnlyBrowser())
        mission = engine.create("metadata", "Nairobi", ["retail_local"], max_searches=1, max_pages_per_entity=1)
        engine.run(mission.id)
        evaluation = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "EVALUATE_ENTITY")
        self.assertEqual("out_of_profile", evaluation.result_summary["state"])
        self.assertIn("beyond SEO metadata", evaluation.result_summary["reason"])

    def test_local_sme_gap_without_positive_local_evidence_needs_review_not_strong(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), ContactGapBrowser())
        mission = engine.create("local", "Nairobi", ["retail_local"], target_profile="local_sme",
                                max_searches=1, max_pages_per_entity=1)
        engine.run(mission.id)
        evaluation = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "EVALUATE_ENTITY")
        self.assertEqual("NEEDS_HUMAN_REVIEW", evaluation.result_summary["target_fit_outcome"])
        report = store.get_mission_report(self.db, mission.id)
        self.assertIn("## Needs Human Review", report)
        self.assertIn("None identified from available evidence.", report)

    def test_local_sme_address_kenyan_contact_and_gap_can_be_strong(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), LocalBusinessGapBrowser())
        mission = engine.create("local", "Nairobi", ["retail_local"], target_profile="local_sme",
                                max_searches=1, max_pages_per_entity=1)
        engine.run(mission.id)
        evaluation = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "EVALUATE_ENTITY")
        self.assertEqual("LIKELY_LOCAL_SME", evaluation.result_summary["target_fit_outcome"])
        report = store.get_mission_report(self.db, mission.id)
        self.assertIn("## Strong Evidence-Backed Opportunities", report)
        self.assertIn("**Target fit:** LIKELY_LOCAL_SME", report)

    def test_explicit_enterprise_signal_is_out_of_profile_for_local_sme(self):
        engine = ScoutMissionEngine(self.db, FakeSearch([SearchHit("Carrefour Kenya", "https://carrefour.example")]), ContactGapBrowser())
        mission = engine.create("local", "Nairobi", ["retail_local"], target_profile="local_sme", max_searches=1)
        engine.run(mission.id)
        with sqlite3.connect(self.db) as conn:
            raw = conn.execute("SELECT data FROM candidates").fetchone()[0]
        self.assertEqual("OUT_OF_PROFILE", json.loads(raw)["target_fit_outcome"])

    def test_corporate_operations_qualifying_lead_and_safe_fit_reason_render(self):
        engine = ScoutMissionEngine(self.db, FakeSearch([SearchHit("Carrefour Kenya", "https://carrefour.example")]), ContactGapBrowser())
        mission = engine.create("operations", "Nairobi", ["retail_local"], target_profile="corporate_operations",
                                max_searches=1, max_pages_per_entity=1)
        engine.run(mission.id)
        report = store.get_mission_report(self.db, mission.id)
        self.assertIn("## Strong Evidence-Backed Opportunities", report)
        self.assertIn("**Target fit:** NEEDS_HUMAN_REVIEW — public operations-system need, contact, and actionable gap observed", report)

    def test_observation_versions_are_retained_and_latest_view_is_distinct(self):
        candidate = __import__("scout.models", fromlist=["Candidate"]).Candidate(name="Acme")
        store.save_candidate(self.db, candidate)
        for timestamp, title in (("2025-01-01T00:00:00+00:00", "Old"), ("2025-02-01T00:00:00+00:00", "New")):
            store.save_source_observation(self.db, __import__("scout.models", fromlist=["SourceObservation"]).SourceObservation(candidate.id, "https://acme.test", timestamp, 200, title, title))
        self.assertEqual(2, len(store.get_source_observations(self.db, candidate.id)))
        latest = store.latest_source_observations(self.db, candidate.id)
        self.assertEqual(1, len(latest)); self.assertEqual("New", latest[0]["title"])

    def test_legacy_observation_schema_upgrades_without_data_loss(self):
        observation = {"candidate_id": "legacy", "source_url": "https://acme.test", "fetched_at": "2025-01-01T00:00:00+00:00", "title": "Old"}
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE source_observations (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, source_url TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(candidate_id, source_url))")
            conn.execute("INSERT INTO source_observations VALUES (?, ?, ?, ?)", ("legacy-row", "legacy", "https://acme.test", json.dumps(observation)))
        self.assertEqual("Old", store.get_source_observations(self.db, "legacy")[0]["title"])
        with sqlite3.connect(self.db) as conn:
            self.assertIn("fetched_at", {row[1] for row in conn.execute("PRAGMA table_info(source_observations)")})

    def test_robots_policy_skip_is_audited(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), FakeBrowser(fail=True))
        mission = engine.create("robots", "Nairobi", ["retail_local"], max_searches=1)
        task = next(task for task in store.tasks_for_mission(self.db, mission.id) if task.task_type == "SEARCH")
        task.status = "SKIPPED"; store.save_task(self.db, task)
        entity = store.resolve_entity(self.db, "Acme", "https://acme.test", "Nairobi", industry="retail_local")
        candidate = __import__("scout.models", fromlist=["Candidate"]).Candidate(name="Acme"); store.save_candidate(self.db, candidate); store.link_candidate_to_entity(self.db, candidate, entity.id)
        class RobotsBrowser(BrowserProvider):
            def fetch(self, url, timeout=15, max_chars=8000): return FetchResult(url, None, None, "", {}, "ROBOTS_DISALLOWED")
        engine.browser = RobotsBrowser(); store.save_task(self.db, __import__("scout.models", fromlist=["ResearchTask"]).ResearchTask(mission_id=mission.id, entity_id=entity.id, task_type="FETCH_PAGE", max_attempts=1, payload={"url": "https://acme.test", "candidate_id": candidate.id}))
        engine.run(mission.id)
        self.assertIn("PAGE_SKIPPED_POLICY", [event["event"] for event in store.mission_events(self.db, mission.id)])
        fetch = next(item for item in store.tasks_for_mission(self.db, mission.id) if item.task_type == "FETCH_PAGE")
        self.assertEqual("SKIPPED", fetch.status); self.assertEqual(1, fetch.attempts)
