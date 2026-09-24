"""Deterministic cross-entity views over Scout's stored research sample."""
from __future__ import annotations

from collections import Counter

from . import alcatrax_knowledge as brain
from . import store


PRIORITY_WEIGHTS = {
    "confidence": {"verified": 35, "observed": 25, "inferred": 8, "unknown": 0},
    "severity": {"high": 30, "medium": 15, "low": 5},
    "source_evidence": 10,
    "solution_pattern": 10,
    "reference_project": 10,
    "capability_maturity": {"proven": 15, "demonstrated": 8, "developing": 4},
}


def _latest_briefs_by_entity(briefs: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for brief in briefs:
        entity_id = brief.get("entity_id", "")
        if entity_id and (entity_id not in latest or brief.get("created_at", "") > latest[entity_id].get("created_at", "")):
            latest[entity_id] = brief
    return list(latest.values())


def _counter(items: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(item for item in items if item).items()))


def _sample_summary(entities, briefs) -> dict:
    opportunities = [opportunity for brief in briefs for opportunity in brief.get("opportunities", [])]
    return {
        "entity_count": len(entities),
        "entities_by_type": _counter([entity.entity_type for entity in entities]),
        "entities_by_industry": _counter([entity.industry for entity in entities]),
        "opportunity_count": len(opportunities),
        "problem_tags": _counter([tag for opportunity in opportunities for tag in opportunity.get("problem_tags", [])]),
        "solution_patterns": _counter([opportunity.get("solution_pattern", "") for opportunity in opportunities]),
        "alcatrax_capabilities": _counter([opportunity.get("alcatrax_capability", "") for opportunity in opportunities]),
        "confidence_distribution": _counter([opportunity.get("confidence", "unknown") for opportunity in opportunities]),
    }


def rank_opportunities(db_path: str, entity_ids: list[str] | None = None) -> list[dict]:
    """Stable and explainable ranking over the latest brief for each entity."""
    briefs = _latest_briefs_by_entity(store.briefs_for_entities(db_path, entity_ids))
    ranked: list[dict] = []
    for brief in briefs:
        for opportunity in brief.get("opportunities", []):
            reasons, score = [], 0
            confidence = opportunity.get("confidence", "unknown")
            confidence_points = PRIORITY_WEIGHTS["confidence"].get(confidence, 0)
            if confidence_points:
                score += confidence_points
                reasons.append(f"{confidence} evidence (+{confidence_points})")
            severity = opportunity.get("severity", "medium")
            severity_points = PRIORITY_WEIGHTS["severity"].get(severity, 0)
            if severity_points:
                score += severity_points
                reasons.append(f"{severity} severity (+{severity_points})")
            if opportunity.get("evidence_url") and opportunity.get("evidence_excerpt"):
                score += PRIORITY_WEIGHTS["source_evidence"]
                reasons.append(f"direct source excerpt (+{PRIORITY_WEIGHTS['source_evidence']})")
            capability = brain.capability_by_id(opportunity.get("alcatrax_capability", ""))
            if capability:
                maturity_points = PRIORITY_WEIGHTS["capability_maturity"][capability.maturity]
                score += maturity_points
                reasons.append(f"{capability.maturity} capability (+{maturity_points})")
            if opportunity.get("solution_pattern"):
                score += PRIORITY_WEIGHTS["solution_pattern"]
                reasons.append(f"matched solution pattern (+{PRIORITY_WEIGHTS['solution_pattern']})")
            if opportunity.get("reference_project"):
                score += PRIORITY_WEIGHTS["reference_project"]
                reasons.append(f"relevant Alcatrax project (+{PRIORITY_WEIGHTS['reference_project']})")
            ranked.append({
                "entity_id": brief.get("entity_id", ""), "candidate_id": brief.get("candidate_id", ""),
                "business_name": brief.get("business_name", ""), "opportunity": opportunity,
                "priority_score": score, "priority_reasons": reasons,
            })
    return sorted(ranked, key=lambda row: (-row["priority_score"], row["entity_id"], row["opportunity"].get("category", "")))


def industry_report(db_path: str, industry: str) -> dict:
    entities = store.list_entities(db_path, industry=industry)
    entity_ids = [entity.id for entity in entities]
    briefs = _latest_briefs_by_entity(store.briefs_for_entities(db_path, entity_ids))
    report = _sample_summary(entities, briefs)
    report.update({
        "industry": industry,
        "scope": f"Based on {len(entities)} researched entities in Scout's stored {industry} sample; not the whole industry.",
        "highest_priority_opportunities": rank_opportunities(db_path, entity_ids),
    })
    return report


def competitor_report(db_path: str) -> dict:
    entities = store.list_entities(db_path, entity_type="competitor")
    briefs = _latest_briefs_by_entity(store.briefs_for_entities(db_path, [entity.id for entity in entities]))
    return {
        "scope": f"Based on {len(entities)} competitor entities in Scout's stored sample; not a complete competitor market map.",
        "competitors": [
            {"entity": entity, "brief": next((brief for brief in briefs if brief.get("entity_id") == entity.id), None)}
            for entity in entities
        ],
    }


def market_summary(db_path: str) -> dict:
    entities = store.list_entities(db_path)
    briefs = _latest_briefs_by_entity(store.briefs_for_entities(db_path, [entity.id for entity in entities]))
    report = _sample_summary(entities, briefs)
    report.update({
        "scope": f"Based on {len(entities)} researched entities in Scout's stored sample; not the whole market.",
        "strongest_opportunities": rank_opportunities(db_path),
    })
    return report
