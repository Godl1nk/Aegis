"""Dangerous-command approval routes.

The agent's bash guard (src/command_approval.py, ported from Hermes) emits
an ``approval_request`` SSE event and blocks until the user resolves it here.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import require_user
from src import command_approval

logger = logging.getLogger(__name__)


class ApprovalDecision(BaseModel):
    choice: str  # 'once' | 'session' | 'always' | 'deny'


def setup_approval_routes() -> APIRouter:
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("")
    async def pending_approvals(request: Request, session_id: str = ""):
        user = require_user(request)
        return {
            "pending": command_approval.list_pending_approvals(
                session_id or None,
                owner=user,
            )
        }

    @router.post("/{approval_id}")
    async def resolve(request: Request, approval_id: str, decision: ApprovalDecision):
        user = require_user(request)
        if decision.choice not in ("once", "session", "always", "deny"):
            raise HTTPException(400, "choice must be once|session|always|deny")
        # Permanent approvals affect every user because the allowlist is
        # process-wide and persisted. Keep that choice admin-only; ordinary
        # users can still approve once or for their own session.
        if decision.choice == "always":
            require_admin(request)
        if not command_approval.resolve_approval(
            approval_id,
            decision.choice,
            owner=user,
        ):
            raise HTTPException(404, "approval not found or already resolved")
        logger.info("Command approval %s resolved: %s", approval_id, decision.choice)
        return {"ok": True, "choice": decision.choice}

    return router
