"""Admin self-update routes: check, download, apply, and roll back Aegis updates."""

import logging
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
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
    def update_apply(request: Request, body: UpdateCommitRequest, background_tasks: BackgroundTasks):
        require_admin(request)
        if not _op_limiter.check(_client(request)):
            raise HTTPException(429, "Too many update operations — try again later")
        commit = (body.commit or "").strip() or None
        force = bool(body.force)
        # Fast synchronous gates so the UI gets actionable errors immediately.
        # The heavy backup/swap/rebuild runs after the response: proxies time
        # out long requests with a bare 502, which used to masquerade as
        # failure while the update was actually proceeding.
        try:
            state = app_update.load_state()
            staged = state.get("staged") or {}
            if commit and commit != staged.get("commit"):
                raise HTTPException(502, "Staged update does not match — download it first")
            if not staged.get("path") or not os.path.exists(staged.get("path")):
                raise HTTPException(502, "No staged update found — download first")
            if app_update._read_rebuild_claim(None) is not None:
                raise HTTPException(409, "A rebuild is already in progress — check logs/rebuild.log instead of stacking another one.")
            if not force:
                live = app_update.active_stream_count()
                if live > 0:
                    raise HTTPException(502,
                                        f"{live} chat stream(s) active — retry when idle or force the "
                                        f"apply (in-flight turns may error)")
            if not app_update.try_claim_apply_slot():
                raise HTTPException(409, "An update apply is already running — check back shortly.")
        except HTTPException:
            raise
        attempt_id = uuid.uuid4().hex
        try:
            background_tasks.add_task(app_update._apply_in_background,
                                      staged["commit"], None, force, attempt_id=attempt_id)
        except Exception:
            app_update._release_apply_slot()
            raise
        return {"accepted": True, "commit": staged["commit"], "attempt_id": attempt_id}

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
