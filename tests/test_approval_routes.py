"""Owner and privilege boundaries for dangerous-command approval routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes.approval_routes import ApprovalDecision, setup_approval_routes
from src import command_approval


def _endpoint(path: str, method: str):
    router = setup_approval_routes()
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def _request(user: str, *, admin: bool = False):
    auth_manager = SimpleNamespace(
        is_configured=True,
        is_admin=lambda candidate: admin and candidate == user,
    )
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(current_user=user, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )


@pytest.mark.asyncio
async def test_pending_route_filters_by_current_user(monkeypatch):
    seen = {}

    def fake_list(session_id=None, *, owner=None):
        seen.update(session_id=session_id, owner=owner)
        return []

    monkeypatch.setattr(command_approval, "list_pending_approvals", fake_list)
    response = await _endpoint("/api/approvals", "GET")(
        _request("alice"), session_id="session-a"
    )

    assert response == {"pending": []}
    assert seen == {"session_id": "session-a", "owner": "alice"}


@pytest.mark.asyncio
async def test_resolve_route_passes_owner_and_permanent_choice_is_admin_only(monkeypatch):
    seen = {}

    def fake_resolve(approval_id, choice, *, owner=None):
        seen.update(approval_id=approval_id, choice=choice, owner=owner)
        return True

    monkeypatch.setattr(command_approval, "resolve_approval", fake_resolve)
    resolve = _endpoint("/api/approvals/{approval_id}", "POST")

    response = await resolve(
        _request("alice"), "approval-a", ApprovalDecision(choice="once")
    )
    assert response == {"ok": True, "choice": "once"}
    assert seen == {
        "approval_id": "approval-a",
        "choice": "once",
        "owner": "alice",
    }

    with pytest.raises(HTTPException) as exc:
        await resolve(
            _request("alice", admin=False),
            "approval-a",
            ApprovalDecision(choice="always"),
        )
    assert exc.value.status_code == 403

    response = await resolve(
        _request("admin", admin=True),
        "approval-admin",
        ApprovalDecision(choice="always"),
    )
    assert response == {"ok": True, "choice": "always"}
    assert seen["owner"] == "admin"
