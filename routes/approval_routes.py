"""Dangerous-command approval routes.

The agent's bash guard (src/command_approval.py, ported from Hermes) emits
an ``approval_request`` SSE event and blocks until the user resolves it here.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import require_user
from src import command_approval

logger = logging.getLogger(__name__)


class ApprovalDecision(BaseModel):
    choice: str  # 'once' | 'session' | 'always' | 'deny'


def setup_approval_routes() -> APIRouter:
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("")
    async def pending_approvals(request: Request, session_id: str = ""):
        require_user(request)
        return {"pending": command_approval.list_pending_approvals(session_id or None)}

    @router.post("/{approval_id}")
    async def resolve(request: Request, approval_id: str, decision: ApprovalDecision):
        require_user(request)
        if decision.choice not in ("once", "session", "always", "deny"):
            raise HTTPException(400, "choice must be once|session|always|deny")
        if not command_approval.resolve_approval(approval_id, decision.choice):
            raise HTTPException(404, "approval not found or already resolved")
        logger.info("Command approval %s resolved: %s", approval_id, decision.choice)
        return {"ok": True, "choice": decision.choice}

    return router
