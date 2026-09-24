from __future__ import annotations

"""Storage: plain SQLite. Deliberately not Postgres/Airtable for v1 --
Anthropic's own guidance is to find the simplest solution and only add
complexity when needed. One file, no server, easy to inspect with any
SQLite browser, trivial to migrate later once Scout is proven useful.

This is the "REMEMBER" step and also what makes "keep watching these five
companies" possible: watched businesses are just rows with watch=1.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import asdict
from urllib.parse import urlparse

from .models import ENTITY_TYPES, Entity

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    entity_id TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    primary_domain TEXT,
    industry TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    normalized_location TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    candidate_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    candidate_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_observations (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(candidate_id, source_url)
);
CREATE TABLE IF NOT EXISTS briefs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    entity_id TEXT,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    watch INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_column(conn, table: str, column_definition: str) -> None:
    column = column_definition.split()[0]
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")


def normalize_domain(url_or_domain: str) -> str:
    """Return a conservative web host identity, treating www as equivalent."""
    value = (url_or_domain or "").strip().lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else "//" + value)
    host = (parsed.hostname or "").rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _normalise_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def validate_entity_type(entity_type: str) -> str:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Invalid entity type '{entity_type}'. Expected one of: {', '.join(ENTITY_TYPES)}")
    return entity_type


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _ensure_column(conn, "candidates", "entity_id TEXT")
    _ensure_column(conn, "briefs", "entity_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_entity_id ON candidates(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_briefs_entity_id ON briefs(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type_industry ON entities(entity_type, industry)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_primary_domain ON entities(primary_domain) WHERE primary_domain IS NOT NULL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_candidate(db_path: str, candidate) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO candidates (id, entity_id, data) VALUES (?, ?, ?)",
            (candidate.id, getattr(candidate, "entity_id", "") or None, json.dumps(asdict(candidate))),
        )


def _entity_from_data(data: dict) -> Entity:
    return Entity(**data)


def get_entity(db_path: str, entity_id: str) -> Entity | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT data FROM entities WHERE id=?", (entity_id,)).fetchone()
        return _entity_from_data(json.loads(row[0])) if row else None


def find_entity_by_domain(db_path: str, domain_or_url: str) -> Entity | None:
    domain = normalize_domain(domain_or_url)
    if not domain:
        return None
    with connect(db_path) as conn:
        row = conn.execute("SELECT data FROM entities WHERE primary_domain=?", (domain,)).fetchone()
        return _entity_from_data(json.loads(row[0])) if row else None


def find_entity_by_identity(db_path: str, canonical_name: str, location: str) -> Entity | None:
    """Exact normalized name and exact normalized non-empty location only."""
    normalized_name, normalized_location = _normalise_text(canonical_name), _normalise_text(location)
    if not normalized_name or not normalized_location:
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM entities WHERE normalized_name=? AND normalized_location=?",
            (normalized_name, normalized_location),
        ).fetchone()
        return _entity_from_data(json.loads(row[0])) if row else None


def save_entity(db_path: str, entity: Entity) -> Entity:
    validate_entity_type(entity.entity_type)
    entity.primary_domain = normalize_domain(entity.primary_domain)
    entity.updated_at = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, canonical_name, normalized_name, entity_type, primary_domain, industry, location, normalized_location, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity.id, entity.canonical_name, _normalise_text(entity.canonical_name), entity.entity_type,
             entity.primary_domain or None, entity.industry, entity.location, _normalise_text(entity.location),
             json.dumps(asdict(entity))),
        )
    return entity


def resolve_entity(db_path: str, canonical_name: str, source_url: str = "", location: str = "",
                   entity_type: str = "prospect", industry: str = "") -> Entity:
    """Domain first, then exact name+location; uncertainty always creates a new entity."""
    validate_entity_type(entity_type)
    domain = normalize_domain(source_url)
    entity = find_entity_by_domain(db_path, domain) if domain else None
    entity = entity or find_entity_by_identity(db_path, canonical_name, location)
    if entity:
        # Do not silently downgrade an explicitly tracked competitor/reference to prospect.
        if entity_type != "prospect":
            entity.entity_type = entity_type
        if canonical_name:
            entity.canonical_name = canonical_name
        if location:
            entity.location = location
        if industry:
            entity.industry = industry
        if domain and not entity.primary_domain:
            entity.primary_domain = domain
        return save_entity(db_path, entity)
    return save_entity(db_path, Entity(canonical_name=canonical_name, entity_type=entity_type,
                                       primary_domain=domain, industry=industry, location=location))


def list_entities(db_path: str, entity_type: str | None = None, industry: str | None = None) -> list[Entity]:
    if entity_type is not None:
        validate_entity_type(entity_type)
    clauses, values = [], []
    if entity_type:
        clauses.append("entity_type=?")
        values.append(entity_type)
    if industry is not None:
        clauses.append("industry=?")
        values.append(industry)
    query = "SELECT data FROM entities" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY canonical_name"
    with connect(db_path) as conn:
        return [_entity_from_data(json.loads(row[0])) for row in conn.execute(query, values).fetchall()]


def link_candidate_to_entity(db_path: str, candidate, entity_id: str) -> None:
    if not get_entity(db_path, entity_id):
        raise ValueError(f"Cannot link candidate to unknown entity '{entity_id}'.")
    candidate.entity_id = entity_id
    save_candidate(db_path, candidate)


def candidates_for_entity(db_path: str, entity_id: str) -> list[dict]:
    with connect(db_path) as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM candidates WHERE entity_id=? ORDER BY rowid", (entity_id,)
        ).fetchall()]


def save_profile(db_path: str, profile) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profiles (candidate_id, data) VALUES (?, ?)",
            (profile.candidate_id, json.dumps(asdict(profile))),
        )


def save_verification(db_path: str, verification) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO verifications (candidate_id, data) VALUES (?, ?)",
            (verification.candidate_id, json.dumps(asdict(verification))),
        )


def save_source_observation(db_path: str, observation) -> None:
    """Persist bounded source evidence so a dossier can be audited later."""
    observation_id = f"{observation.candidate_id}:{observation.source_url}"
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO source_observations (id, candidate_id, source_url, data) VALUES (?, ?, ?, ?)",
            (observation_id, observation.candidate_id, observation.source_url, json.dumps(asdict(observation))),
        )


def get_source_observations(db_path: str, candidate_id: str) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM source_observations WHERE candidate_id=? ORDER BY rowid", (candidate_id,)
        ).fetchall()
        return [json.loads(row[0]) for row in rows]


def load_dossier(db_path: str, candidate_id: str) -> dict:
    """Reload persisted profile, verification, and source evidence together."""
    with connect(db_path) as conn:
        def one(table: str):
            row = conn.execute(f"SELECT data FROM {table} WHERE candidate_id=?", (candidate_id,)).fetchone()
            return json.loads(row[0]) if row else None
        candidate_row = conn.execute("SELECT data FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return {
            "candidate": json.loads(candidate_row[0]) if candidate_row else None,
            "profile": one("profiles"),
            "verification": one("verifications"),
            "source_observations": [json.loads(row[0]) for row in conn.execute(
                "SELECT data FROM source_observations WHERE candidate_id=? ORDER BY rowid", (candidate_id,)
            ).fetchall()],
        }


def save_brief(db_path: str, brief) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO briefs (id, candidate_id, entity_id, data, status, watch) "
            "VALUES (?, ?, ?, ?, ?, COALESCE((SELECT watch FROM briefs WHERE id=?), 0))",
            (brief.id, brief.candidate_id, getattr(brief, "entity_id", "") or None,
             json.dumps(asdict(brief)), brief.status, brief.id),
        )


def set_watch(db_path: str, brief_id: str, watch: bool) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE briefs SET watch=? WHERE id=?", (1 if watch else 0, brief_id))


def get_watched_briefs(db_path: str) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT data FROM briefs WHERE watch=1").fetchall()
        return [json.loads(r[0]) for r in rows]


def list_briefs(db_path: str) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT data FROM briefs ORDER BY rowid DESC").fetchall()
        return [json.loads(r[0]) for r in rows]


def briefs_for_entities(db_path: str, entity_ids: list[str] | None = None) -> list[dict]:
    with connect(db_path) as conn:
        if entity_ids is None:
            rows = conn.execute("SELECT data FROM briefs WHERE entity_id IS NOT NULL ORDER BY rowid DESC").fetchall()
        elif not entity_ids:
            return []
        else:
            placeholders = ", ".join("?" for _ in entity_ids)
            rows = conn.execute(
                f"SELECT data FROM briefs WHERE entity_id IN ({placeholders}) ORDER BY rowid DESC", entity_ids
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
