import tempfile
import threading
import time
import json
import sqlite3
import unittest
from pathlib import Path

from scout import store
from scout.mission import DomainPoliteness, ScoutMissionEngine, plan_queries, prioritise_pages
from scout.models import Mission
from scout.providers.base import BrowserProvider, FetchResult, SearchHit, SearchProvider


class FakeSearch(SearchProvider):
    def __init__(self, hits=None): self.hits = hits or [SearchHit("Acme Hardware", "https://acme.test", "tools")]
    def search(self, query, num_results=10): return self.hits


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

    def test_failed_page_retries_then_mission_continues(self):
        engine = ScoutMissionEngine(self.db, FakeSearch(), FakeBrowser(fail=True))
        mission = engine.create("Find hardware", "Nairobi", ["retail_local"], max_searches=1, max_retries=1)
        done = engine.run(mission.id)
        failures = [task for task in store.tasks_for_mission(self.db, mission.id) if task.status == "FAILED"]
        self.assertTrue(failures); self.assertEqual(2, failures[0].attempts); self.assertIn(done.status, ("STOPPED", "COMPLETED"))

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
