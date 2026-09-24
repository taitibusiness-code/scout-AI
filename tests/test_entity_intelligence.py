import tempfile
import unittest
from pathlib import Path

from scout import intelligence, store
from scout.models import Candidate, OpportunityFinding, ProspectBrief, SourceFact, SourceObservation


def strong_opportunity():
    return OpportunityFinding(
        category="ecommerce", observed="No catalogue found.", evidence_url="https://acme.test/products",
        evidence_excerpt="No products are listed.", business_consequence="Manual discovery.", opportunity="Build a catalogue.",
        problem_tags=["no_catalogue"], solution_pattern="automotive_digital_catalogue",
        alcatrax_capability="catalogue", reference_project="Lucky Line Autospares", severity="high", confidence="observed",
    )


def weak_opportunity():
    return OpportunityFinding(
        category="other", observed="Possible issue.", evidence_url="", evidence_excerpt="",
        business_consequence="Unclear.", opportunity="Investigate.", severity="medium", confidence="inferred",
    )


class EntityIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "scout.db")

    def tearDown(self):
        self.temp.cleanup()

    def add_research(self, name, domain, industry="automotive_spare_parts", entity_type="prospect", opportunities=None):
        entity = store.resolve_entity(self.db, name, domain, "Nairobi", entity_type, industry)
        candidate = Candidate(name=name, source_url=domain)
        store.save_candidate(self.db, candidate)
        store.link_candidate_to_entity(self.db, candidate, entity.id)
        brief = ProspectBrief(candidate_id=candidate.id, entity_id=entity.id, business_name=name,
                              opportunities=opportunities or [])
        store.save_brief(self.db, brief)
        return entity, candidate, brief

    def test_new_candidate_creates_default_prospect_entity(self):
        entity = store.resolve_entity(self.db, "Acme Motors", "https://acme.test", "Nairobi")
        self.assertEqual("prospect", entity.entity_type)
        self.assertEqual("acme.test", entity.primary_domain)

    def test_normalized_domain_www_and_pages_resolve_to_one_entity(self):
        first = store.resolve_entity(self.db, "Acme", "https://www.acme.test/about", "Nairobi")
        second = store.resolve_entity(self.db, "Acme Motors", "http://acme.test/contact", "Nairobi")
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(store.list_entities(self.db)))

    def test_name_and_compatible_location_resolves_without_domain(self):
        first = store.resolve_entity(self.db, "ABC Motors", "", "Nairobi")
        second = store.resolve_entity(self.db, "abc motors", "", "nairobi")
        self.assertEqual(first.id, second.id)

    def test_incompatible_or_uncertain_identity_stays_separate(self):
        nairobi = store.resolve_entity(self.db, "ABC Motors", "", "Nairobi")
        mombasa = store.resolve_entity(self.db, "ABC Motors", "", "Mombasa")
        unknown_a = store.resolve_entity(self.db, "Uncertain Motors", "", "")
        unknown_b = store.resolve_entity(self.db, "Uncertain Motors", "", "")
        self.assertNotEqual(nairobi.id, mombasa.id)
        self.assertNotEqual(unknown_a.id, unknown_b.id)

    def test_explicit_entity_classifications_and_invalid_type(self):
        competitor = store.resolve_entity(self.db, "Vantra", "https://vantra.test", "Nairobi", "competitor")
        reference = store.resolve_entity(self.db, "Association", "https://association.test", "Nairobi", "industry_reference")
        self.assertEqual("competitor", competitor.entity_type)
        self.assertEqual("industry_reference", reference.entity_type)
        with self.assertRaises(ValueError):
            store.resolve_entity(self.db, "Bad", "https://bad.test", entity_type="invalid")

    def test_multiple_candidates_link_to_one_entity_and_keep_provenance(self):
        entity = store.resolve_entity(self.db, "Acme", "https://acme.test", "Nairobi")
        first, second = Candidate(name="Acme", source_url="https://acme.test/a"), Candidate(name="Acme", source_url="https://acme.test/b")
        for candidate in (first, second):
            store.save_candidate(self.db, candidate)
            store.link_candidate_to_entity(self.db, candidate, entity.id)
        source = SourceObservation(first.id, "https://acme.test/a", "now", 200, "Acme", "Acme Nairobi",
                                   facts=[SourceFact("location", "Nairobi", "https://acme.test/a", "Nairobi", "now", "observed")])
        store.save_source_observation(self.db, source)
        self.assertEqual(2, len(store.candidates_for_entity(self.db, entity.id)))
        self.assertEqual("Nairobi", store.load_dossier(self.db, first.id)["source_observations"][0]["facts"][0]["value"])

    def test_opportunity_provenance_survives_entity_linking(self):
        entity, candidate, _ = self.add_research("Acme", "https://acme.test", opportunities=[strong_opportunity()])
        saved = store.briefs_for_entities(self.db, [entity.id])[0]
        self.assertEqual(entity.id, saved["entity_id"])
        self.assertEqual("https://acme.test/products", saved["opportunities"][0]["evidence_url"])

    def test_industry_report_uses_only_stored_matching_sample(self):
        for index in range(5):
            self.add_research(f"Auto {index}", f"https://auto{index}.test", opportunities=[strong_opportunity()])
        self.add_research("Vantra", "https://vantra.test", entity_type="competitor", opportunities=[weak_opportunity()])
        self.add_research("Interior Studio", "https://interior.test", industry="interiors", opportunities=[weak_opportunity()])
        report = intelligence.industry_report(self.db, "automotive_spare_parts")
        self.assertEqual(6, report["entity_count"])
        self.assertEqual(5, report["entities_by_type"]["prospect"])
        self.assertEqual(1, report["entities_by_type"]["competitor"])
        self.assertNotIn("interiors", report["entities_by_industry"])
        self.assertIn("stored automotive_spare_parts sample", report["scope"])

    def test_ranking_is_stable_explainable_and_favors_stronger_evidence(self):
        self.add_research("Strong", "https://strong.test", opportunities=[strong_opportunity()])
        self.add_research("Weak", "https://weak.test", opportunities=[weak_opportunity()])
        first = intelligence.rank_opportunities(self.db)
        second = intelligence.rank_opportunities(self.db)
        self.assertEqual(first, second)
        self.assertGreater(first[0]["priority_score"], first[1]["priority_score"])
        self.assertTrue(first[0]["priority_reasons"])
        self.assertIn("direct source excerpt", " ".join(first[0]["priority_reasons"]))

    def test_competitor_report_and_market_summary_are_entity_views(self):
        self.add_research("Prospect", "https://prospect.test", opportunities=[strong_opportunity()])
        competitor, _, _ = self.add_research("Vantra", "https://vantra.test", entity_type="competitor", opportunities=[weak_opportunity()])
        report = intelligence.competitor_report(self.db)
        self.assertEqual([competitor.id], [row["entity"].id for row in report["competitors"]])
        market = intelligence.market_summary(self.db)
        self.assertEqual({"competitor": 1, "prospect": 1}, market["entities_by_type"])
        self.assertIn("stored sample", market["scope"])

    def test_duplicate_discovery_does_not_double_count_entity(self):
        self.add_research("Acme", "https://www.acme.test/one", opportunities=[strong_opportunity()])
        self.add_research("Acme", "https://acme.test/two", opportunities=[strong_opportunity()])
        report = intelligence.market_summary(self.db)
        self.assertEqual(1, report["entity_count"])

