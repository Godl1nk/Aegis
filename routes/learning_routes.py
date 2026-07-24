"""Learning journey routes — graph + rendered timeline for the Journey panel.

Ports Hermes' "learning made visible" surface: GET /api/learning/graph
returns the raw node/edge payload; GET /api/learning/journey returns the
pre-rendered timeline frames (style runs) the frontend paints directly.
"""

import logging

from fastapi import APIRouter, Request

from src.auth_helpers import get_current_user, require_user
from services.memory.learning_graph import build_learning_graph
from services.memory.learning_graph_render import render_frames

logger = logging.getLogger(__name__)


def setup_learning_routes(skills_manager, memory_manager) -> APIRouter:
    router = APIRouter(prefix="/api/learning", tags=["learning"])

    @router.get("/graph")
    async def learning_graph(request: Request):
        require_user(request)
        owner = get_current_user(request)
        return build_learning_graph(skills_manager, memory_manager, owner=owner)

    @router.get("/journey")
    async def learning_journey(
        request: Request, cols: int = 80, rows: int = 16, frames: int = 48
    ):
        require_user(request)
        owner = get_current_user(request)
        payload = build_learning_graph(skills_manager, memory_manager, owner=owner)
        return render_frames(
            payload,
            cols=max(44, min(int(cols), 200)),
            rows=max(14, min(int(rows), 60)),
            frames=frames,
        )

    return router
