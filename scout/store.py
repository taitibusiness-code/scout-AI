from __future__ import annotations

"""Storage: plain SQLite. Deliberately not Postgres/Airtable for v1 --
Anthropic's own guidance is to find the simplest solution and only add
complexity when needed. One file, no server, easy to inspect with any
SQLite browser, trivial to migrate later once Scout is proven useful.

This is the "REMEMBER" step and also what makes "keep watching these five
companies" possible: watched businesses are just rows with watch=1.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
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
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    watch INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_candidate(db_path: str, candidate) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO candidates (id, data) VALUES (?, ?)",
            (candidate.id, json.dumps(asdict(candidate))),
        )


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
            "INSERT OR REPLACE INTO briefs (id, candidate_id, data, status, watch) "
            "VALUES (?, ?, ?, ?, COALESCE((SELECT watch FROM briefs WHERE id=?), 0))",
            (brief.id, brief.candidate_id, json.dumps(asdict(brief)), brief.status, brief.id),
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
