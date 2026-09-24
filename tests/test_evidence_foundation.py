import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scout import analyze, config, pipeline, store, verify
from scout.extract import profile_from_observations
from scout.models import BusinessProfile, PageSnapshot, SourceFact, SourceObservation
from scout.providers.base import BrowserProvider, FetchResult, LLMProvider, LLMToolResult, SearchHit, SearchProvider


def observation(url, field="location", value="Nairobi", confidence="observed", excerpt="Nairobi"):
    return SourceObservation(
        candidate_id="candidate", source_url=url, fetched_at="2026-01-01T00:00:00+00:00",
        status_code=200, title="Acme", evidence_excerpt=excerpt,
        facts=[SourceFact(field, value, url, excerpt, "2026-01-01T00:00:00+00:00", confidence)],
    )


def snapshot(url, text):
    return PageSnapshot(url, "2026-01-01T00:00:00+00:00", 200, "Acme", text,
                        {"has_viewport_tag": True, "meta_description": "present",
                         "has_whatsapp_link": True, "has_ecommerce_words": True})


def profile():
    return BusinessProfile("candidate", "Acme", "parts", "drivers", "Nairobi", ["whatsapp"], True, ["https://one"] , "")


class OpportunityLLM(LLMProvider):
    def __init__(self, item):
        self.item = item

    def extract(self, **kwargs):
        return LLMToolResult(kwargs["tool_name"], {"opportunities": [self.item]})


def opportunity_item(**changes):
    item = {
        "category": "ecommerce", "observed": "The business sells brake pads.",
        "evidence_url": "https://two", "evidence_excerpt": "We sell brake pads.",
        "business_consequence": "Customers need product information.",
        "opportunity": "Create a catalogue.", "alcatrax_capability": "catalogue",
        "reference_project": "Lucky Line Autospares", "severity": "medium", "confidence": "observed",
    }
    item.update(changes)
    return item


class EvidenceVerificationTests(unittest.TestCase):
    def test_one_source_is_observed_never_verified(self):
        result = verify.verify("candidate", [observation("https://one")])
        fact = next(f for f in result.facts if f.fact == "location")
        self.assertEqual("observed", fact.confidence)
        self.assertEqual(["https://one"], fact.sources)

    def test_two_distinct_sources_are_verified(self):
        result = verify.verify("candidate", [observation("https://one"), observation("https://two")])
        fact = next(f for f in result.facts if f.fact == "location")
        self.assertEqual("verified", fact.confidence)
        self.assertEqual(2, len(fact.evidence))

    def test_aggregate_profile_with_two_urls_cannot_verify(self):
        legacy = BusinessProfile("candidate", "Acme", "parts", "drivers", "Nairobi", [], None,
                                 ["https://one", "https://two"], "")
        result = verify.verify("candidate", [legacy])
        fact = next(f for f in result.facts if f.fact == "location")
        self.assertEqual("inferred", fact.confidence)

    def test_conflicts_never_verify(self):
        result = verify.verify("candidate", [observation("https://one", value="Nairobi"),
                                               observation("https://two", value="Mombasa")])
        fact = next(f for f in result.facts if f.fact == "location")
        self.assertNotEqual("verified", fact.confidence)
        self.assertTrue(result.conflicts)

    def test_unknown_remains_unknown(self):
        result = verify.verify("candidate", [SourceObservation("candidate", "https://one", "now", 200, "Acme", "")])
        fact = next(f for f in result.facts if f.fact == "location")
        self.assertEqual("unknown", fact.confidence)

    def test_observation_evidence_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "scout.db")
            source = observation("https://one", excerpt="Acme is in Nairobi")
            store.save_source_observation(db, source)
            saved = store.get_source_observations(db, "candidate")
            self.assertEqual("Acme is in Nairobi", saved[0]["facts"][0]["evidence_excerpt"])
            self.assertEqual("https://one", store.load_dossier(db, "candidate")["source_observations"][0]["source_url"])


class OpportunityAttributionTests(unittest.TestCase):
    def test_opportunity_uses_its_actual_source(self):
        findings = analyze.analyze("candidate", [snapshot("https://one", "About Acme."),
                                                   snapshot("https://two", "We sell brake pads.")],
                                   profile(), OpportunityLLM(opportunity_item()))
        self.assertEqual("https://two", findings[0].evidence_url)
        self.assertEqual("We sell brake pads.", findings[0].evidence_excerpt)

    def test_unsupported_opportunity_evidence_is_rejected(self):
        findings = analyze.analyze("candidate", [snapshot("https://one", "About Acme.")], profile(),
                                   OpportunityLLM(opportunity_item(evidence_url="https://one", evidence_excerpt="Not on the page.")))
        self.assertEqual([], findings)

    def test_invented_capability_is_rejected(self):
        findings = analyze.analyze("candidate", [snapshot("https://two", "We sell brake pads.")], profile(),
                                   OpportunityLLM(opportunity_item(alcatrax_capability="invented")))
        self.assertEqual("", findings[0].alcatrax_capability)

    def test_invented_project_is_rejected(self):
        findings = analyze.analyze("candidate", [snapshot("https://two", "We sell brake pads.")], profile(),
                                   OpportunityLLM(opportunity_item(reference_project="Invented Client")))
        self.assertEqual("", findings[0].reference_project)

    def test_known_capability_and_project_work(self):
        findings = analyze.analyze("candidate", [snapshot("https://two", "We sell brake pads.")], profile(),
                                   OpportunityLLM(opportunity_item()))
        self.assertEqual("catalogue", findings[0].alcatrax_capability)
        self.assertEqual("Lucky Line Autospares", findings[0].reference_project)


class FakeSearch(SearchProvider):
    def search(self, query, num_results=10):
        return [SearchHit("Acme Parts - Nairobi", "https://one")]


class FakeBrowser(BrowserProvider):
    def fetch(self, url, timeout=15, max_chars=8000):
        return FetchResult(url, 200, "Acme Parts", "Acme Parts sells brake pads in Nairobi.",
                           {"has_viewport_tag": True, "meta_description": "present",
                            "has_whatsapp_link": True, "has_ecommerce_words": True})


class FakePipelineLLM(LLMProvider):
    def extract(self, system, user_content, tool_name, tool_description, input_schema, max_tokens=1000):
        if tool_name == "record_business_profile":
            return LLMToolResult(tool_name, {
                "business_name": "Acme Parts", "what_they_sell": "brake pads", "target_customers": "drivers",
                "location": "Nairobi", "contact_channels": ["whatsapp"], "has_ecommerce": True,
                "confidence_note": "", "evidence": {
                    "business_name": "Acme Parts", "what_they_sell": "sells brake pads", "target_customers": "", "location": "Nairobi",
                    "contact_channels": "", "has_ecommerce": "",
                },
            })
        return LLMToolResult(tool_name, {"opportunities": []})


class PipelineTests(unittest.TestCase):
    def test_fake_provider_end_to_end_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            result = pipeline.run("parts", FakeSearch(), FakeBrowser(), FakePipelineLLM(),
                                  str(Path(directory) / "scout.db"), str(Path(directory) / "log.jsonl"))
            self.assertEqual(1, result.discovered)
            self.assertEqual(1, result.completed)
            self.assertEqual([], result.failures)
            dossier = store.load_dossier(str(Path(directory) / "scout.db"), result.briefs[0]["brief"].candidate_id)
            self.assertEqual(1, len(dossier["source_observations"]))

    def test_non_llm_config_does_not_require_anthropic_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.load_config(require_llm=False).anthropic_api_key)
