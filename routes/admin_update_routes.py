"""Admin self-update routes: check, download, apply, and roll back Aegis updates."""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import app_update
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class UpdateRefRequest(BaseModel):
    ref: str | None = None


class UpdateCommitRequest(BaseModel):
    commit: str | None = None
    force: bool = False


class AutoUpdateBody(BaseModel):
    enabled: bool = False
    start_hour: int = 2
    end_hour: int = 6


def setup_admin_update_routes() -> APIRouter:
    router = APIRouter(prefix="/api/admin/updates")
    _check_limiter = RateLimiter(max_requests=10, window_seconds=3600)
    _op_limiter = RateLimiter(max_requests=5, window_seconds=3600)

    def _client(request: Request) -> str:
        try:
            return request.client.host
        except Exception:
            return "unknown"

    @router.get("/status")
    def update_status(request: Request):
        require_admin(request)
        return app_update.status()

    @router.post("/check")
    def update_check(request: Request):
        require_admin(request)
        if not _check_limiter.check(_client(request)):
            raise HTTPException(429, "Too many update checks — try again later")
        try:
            return app_update.check_for_updates(force=True)
        except app_update.UpdateError as e:
            raise HTTPException(502, str(e))

    @router.post("/download")
    def update_download(request: Request, body: UpdateRefRequest):
        require_admin(request)
        if not _op_limiter.check(_client(request)):
            raise HTTPException(429, "Too many update operations — try again later")
        try:
            return app_update.download_update((body.ref or "").strip() or None)
        except app_update.UpdateBusy as e:
            raise HTTPException(409, str(e))
        except app_update.UpdateError as e:
            raise HTTPException(502, str(e))

    @router.post("/apply")
    def update_apply(request: Request, body: UpdateCommitRequest):
        require_admin(request)
        if not _op_limiter.check(_client(request)):
            raise HTTPException(429, "Too many update operations — try again later")
        try:
            return app_update.apply_staged((body.commit or "").strip() or None,
                                           force=bool(body.force))
        except app_update.UpdateBusy as e:
            raise HTTPException(409, str(e))
        except app_update.UpdateError as e:
            raise HTTPException(502, str(e))

    @router.post("/rollback")
    def update_rollback(request: Request):
        require_admin(request)
        if not _op_limiter.check(_client(request)):
            raise HTTPException(429, "Too many update operations — try again later")
        try:
            return app_update.rollback()
        except app_update.UpdateBusy as e:
            raise HTTPException(409, str(e))
        except app_update.UpdateError as e:
            raise HTTPException(502, str(e))

    @router.get("/auto")
    def update_auto_get(request: Request):
        require_admin(request)
        cfg = app_update.auto_update_settings()
        try:
            last = app_update.load_state().get("last_auto")
        except Exception:
            last = None
        return {**cfg, "last": last}

    @router.post("/auto")
    def update_auto_set(request: Request, body: AutoUpdateBody):
        require_admin(request)
        start, end = int(body.start_hour), int(body.end_hour)
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise HTTPException(400, "maintenance window hours must be 0-23")
        if start == end:
            raise HTTPException(400, "maintenance window must not be empty (start != end)")
        from src.settings import load_settings, save_settings
        current = load_settings()
        current["auto_update_enabled"] = bool(body.enabled)
        current["auto_update_start_hour"] = start
        current["auto_update_end_hour"] = end
        save_settings(current)
        return {"enabled": bool(body.enabled), "start_hour": start, "end_hour": end}

    return router
