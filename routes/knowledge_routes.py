"""Owner-scoped Brain knowledge management; no unvalidated text editing."""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from src.auth_helpers import require_privilege
from src.settings import get_setting

logger = logging.getLogger(__name__)


def setup_knowledge_routes(memory_manager, memory_vector=None):
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

    def owner_for(request):
        # None means unclaimed in the store, never "all users" in these APIs.
        return require_privilege(request, "can_manage_memory") or None

    def existing(memory_id, owner):
        rows = memory_manager.load_by_ids([memory_id], owner=owner)
        item = next((row for row in rows if row.get("id") == memory_id
                     and row.get("owner") == owner and row.get("kind") == "knowledge"
                     and row.get("status", "active") == "active"), None)
        if item is None:
            raise HTTPException(404, "Knowledge entry not found")
        return item

    @router.get("")
    def list_knowledge(request: Request, q: str = Query("", max_length=500),
                       freshness: Literal["all", "fresh", "expired"] = "all",
                       offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        owner = owner_for(request)
        result = memory_manager.query_knowledge(owner, query=q, freshness=freshness, offset=offset, limit=limit)
        enabled = get_setting("knowledge_auto_learn_enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        result["learning_enabled"] = bool(enabled)
        return result

    @router.delete("/{knowledge_id}")
    def delete_knowledge(knowledge_id: str, request: Request):
        owner = owner_for(request)
        existing(knowledge_id, owner)
        if not memory_manager.delete_knowledge(knowledge_id, owner=owner):
            raise HTTPException(404, "Knowledge entry not found")
        if memory_vector and getattr(memory_vector, "healthy", False):
            try:
                memory_vector.remove(knowledge_id)
            except Exception:
                # Storage deletion is authoritative; stale hits cannot hydrate.
                logger.warning("Knowledge vector cleanup failed for %s", knowledge_id, exc_info=True)
        return {"ok": True, "knowledge_id": knowledge_id}

    @router.post("/{knowledge_id}/revalidate")
    async def revalidate_knowledge(knowledge_id: str, request: Request):
        owner = owner_for(request)
        require_privilege(request, "can_use_research")
        from src.tool_security import blocked_tools_for_owner
        blocked = set(get_setting("disabled_tools", []) or []) | blocked_tools_for_owner(owner)
        if blocked & {"manage_knowledge", "web_search", "web_fetch"}:
            raise HTTPException(403, "Knowledge validation or web tools are disabled for this account")
        item = await run_in_threadpool(existing, knowledge_id, owner)
        from src.agent_tools.knowledge_tools import KnowledgeTool
        result = await KnowledgeTool()._learn(
            {"claim": item["text"], "query": item.get("query") or item["text"]},
            owner, memory_manager, memory_vector,
        )
        if not result.get("validated"):
            raise HTTPException(422, result.get("error") or "Revalidation failed; entry unchanged")
        return result

    return router
