"""DennisOS Action Registry -- stub.

Nothing calls this yet (Scout v1/Phase-1 has no write/external/destructive
path at all). It exists now so that when Developer/Sales agents start
proposing real actions (send email, send WhatsApp, submit a form, modify a
client site, deploy code, purchase something), there's already a single
choke point they must go through -- rather than each new agent inventing its
own ad-hoc permission check.

Flow this enforces:
    agent proposes -> registry checks tier -> permission gate -> Dennis
    approves -> execute -> audit log

Tiers, matching the DennisOS model:
    READ_ONLY    - automatic, no approval needed (search, fetch, extract, store)
    EXTERNAL     - requires explicit approval every time (send message, submit
                   form, create account)
    DESTRUCTIVE  - requires stronger approval / blocked by default (modify
                   client site, deploy code, purchase, delete data)
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path


class Tier(str, Enum):
    READ_ONLY = "read_only"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


@dataclass
class ActionRequest:
    agent: str            # e.g. "scout", "developer", "sales"
    action: str            # e.g. "send_whatsapp_message"
    tier: Tier
    detail: dict = field(default_factory=dict)
    approved: bool = False
    executed: bool = False


class ActionRegistry:
    """Deny-by-default. Registering an action does not execute it -- it only
    logs the proposal and, for READ_ONLY, marks it auto-approved. Everything
    else sits pending until something outside this class (a human, for now --
    never another agent) flips `approved=True`.
    """

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)

    def propose(self, request: ActionRequest) -> ActionRequest:
        if request.tier == Tier.READ_ONLY:
            request.approved = True  # automatic tier
        self._log("proposed", request)
        return request

    def approve(self, request: ActionRequest) -> ActionRequest:
        """Only ever called from a human-driven path (CLI prompt, DennisOS
        approval UI later) -- never called by an agent on its own request."""
        request.approved = True
        self._log("approved", request)
        return request

    def execute(self, request: ActionRequest) -> ActionRequest:
        if not request.approved:
            raise PermissionError(
                f"Action '{request.action}' by '{request.agent}' is tier={request.tier.value} "
                f"and was never approved. Refusing to execute."
            )
        if request.tier == Tier.DESTRUCTIVE:
            raise PermissionError(
                f"DESTRUCTIVE actions are blocked in this stub. '{request.action}' by "
                f"'{request.agent}' needs a real execution path implemented and reviewed "
                f"before this tier can run -- not just an approval flag."
            )
        request.executed = True
        self._log("executed", request)
        return request

    def _log(self, event: str, request: ActionRequest):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "agent": request.agent,
            "action": request.action,
            "tier": request.tier.value,
            "approved": request.approved,
            "executed": request.executed,
            "detail": request.detail,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
