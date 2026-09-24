from __future__ import annotations

"""Structured logging: every pipeline action gets one JSON line.

This is the "everything important is logged" principle from DennisOS made
concrete. Nothing here is optional or best-effort -- if a step ran, it logged.
Log format is JSONL so it's trivially greppable/loadable into a dataframe later.
"""
import json
import time
from pathlib import Path


class ActionLog:
    def __init__(self, path: str):
        self.path = Path(path)

    def log(self, step: str, candidate_id: str | None, action: str, detail: dict):
        record = {
            "ts": time.time(),
            "step": step,           # discover|browse|understand|verify|analyze|remember|recommend
            "candidate_id": candidate_id,
            "action": action,
            "detail": detail,
            "tier": "read_only",    # v1: everything Scout does is read-only/automatic
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
