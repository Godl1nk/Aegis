"""Editing a skill from the card form: rename, field updates, and the sections
the form doesn't own surviving the round trip.

The expanded skill card used to expose only a raw SKILL.md textarea. It now
edits name / title / when-to-use / how / tags as fields and PUTs them, which
put weight on three things that were previously untested:

  * PUT must report the resulting slug — it slugifies the submitted name, and
    a rename moves the skill directory, so the UI has to re-target the card,
    the markdown cache and every later request at the new id.
  * A rename onto an existing skill must not read as "not found".
  * Pitfalls / Verification / extra notes aren't in the form. Every save is a
    read-modify-write, so they have to survive one.
"""
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.datastructures import State

from routes.skills_routes import SkillUpdateRequest, setup_skills_routes
from services.memory.skill_format import Skill
from services.memory.skills import SkillsManager


def _request(user: str) -> Request:
    scope = {"type": "http", "headers": [], "method": "PUT", "path": "/", "query_string": b""}
    req = Request(scope)
    req.state._state.update({"current_user": user})
    req.scope["app"] = type("A", (), {"state": State()})()
    return req


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.fixture()
def api(tmp_path):
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    return sm, _route(router, "/api/skills/{skill_id}", "PUT")


def _add(sm: SkillsManager, name: str, owner: str = "alice", **kw) -> None:
    skill_dir = Path(sm.skills_root) / "general" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    sk = Skill(name=name, description=kw.pop("description", "d"), category="general",
               owner=owner, **kw)
    (skill_dir / "SKILL.md").write_text(sk.to_markdown(), encoding="utf-8")


@pytest.mark.asyncio
async def test_put_reports_the_slugified_name_after_rename(api):
    sm, put = api
    _add(sm, "old-name")

    result = await put(_request("alice"), "old-name", SkillUpdateRequest(name="Check Temp"))

    assert result == {"ok": True, "name": "check-temp"}
    names = [s["name"] for s in sm.load(owner="alice")]
    assert "check-temp" in names and "old-name" not in names


@pytest.mark.asyncio
async def test_put_reports_the_name_even_without_a_rename(api):
    """The UI reads `name` off every save, not just renames."""
    sm, put = api
    _add(sm, "stable")

    result = await put(_request("alice"), "stable", SkillUpdateRequest(description="new title"))

    assert result == {"ok": True, "name": "stable"}


@pytest.mark.asyncio
async def test_rename_onto_an_existing_skill_is_a_conflict(api):
    sm, put = api
    _add(sm, "first")
    _add(sm, "second")

    with pytest.raises(HTTPException) as exc:
        await put(_request("alice"), "first", SkillUpdateRequest(name="second"))

    assert exc.value.status_code == 409
    # Both survive: a failed rename must not consume either skill.
    assert {"first", "second"} <= {s["name"] for s in sm.load(owner="alice")}


@pytest.mark.asyncio
async def test_form_fields_update_without_touching_the_rest(api):
    """The form owns name/description/when_to_use/procedure/tags. Anything
    else on the skill must come back unchanged."""
    sm, put = api
    _add(sm, "keeper", pitfalls=["mind the gap"], verification=["it ran"],
         body_extra="a longer note")

    await put(_request("alice"), "keeper", SkillUpdateRequest(
        description="edited", when_to_use="when x", procedure=["a", "b"], tags=["t"],
    ))

    sk = next(s for s in sm.load(owner="alice") if s["name"] == "keeper")
    assert sk["description"] == "edited"
    assert sk["procedure"] == ["a", "b"]
    assert sk["tags"] == ["t"]
    assert sk["pitfalls"] == ["mind the gap"]
    assert sk["verification"] == ["it ran"]
    assert sk["body_extra"] == "a longer note"


def test_body_extra_survives_a_markdown_round_trip():
    """It was emitted as bare trailing prose, so re-reading the file folded it
    into the last section's final bullet and body_extra came back empty — the
    text was destroyed by the first edit of any skill that had both."""
    sk = Skill(name="x", description="d", verification=["check it"],
               body_extra="extra prose\nsecond line")

    back = Skill.from_markdown(sk.to_markdown())

    assert back.verification == ["check it"]
    assert back.body_extra == "extra prose\nsecond line"
    # And it's a fixed point, not just one clean hop.
    again = Skill.from_markdown(back.to_markdown())
    assert again.body_extra == back.body_extra
    assert again.verification == back.verification


@pytest.mark.asyncio
async def test_required_tools_save_and_clear_without_losing_other_fields(api):
    sm, put = api
    _add(sm, "tools", requires_toolsets=["legacy_tool"], pitfalls=["preserve this"])
    await put(_request("alice"), "tools", SkillUpdateRequest(
        requires_toolsets=["legacy_tool", "web_search", "manage_knowledge"],
    ))
    sk = next(s for s in sm.load(owner="alice") if s["name"] == "tools")
    assert sk["requires_toolsets"] == ["legacy_tool", "web_search", "manage_knowledge"]
    assert sk["pitfalls"] == ["preserve this"]
    await put(_request("alice"), "tools", SkillUpdateRequest(requires_toolsets=[]))
    sk = next(s for s in sm.load(owner="alice") if s["name"] == "tools")
    assert sk["requires_toolsets"] == []


@pytest.mark.asyncio
async def test_tool_catalog_includes_knowledge_and_disabled_tools(tmp_path, monkeypatch):
    import src.settings as settings
    import src.tool_security as security
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: ["web_search"] if key == "disabled_tools" else default)
    monkeypatch.setattr(security, "blocked_tools_for_owner", lambda owner: {"web_fetch"})
    catalog = _route(setup_skills_routes(SkillsManager(str(tmp_path))), "/api/skills/tool-options", "GET")
    result = await catalog(_request("alice"))
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert "manage_knowledge" in tools
    assert tools["web_search"]["unavailable"] is True
    assert tools["web_fetch"]["unavailable"] is True
    assert tools["manage_knowledge"]["unavailable"] is False


@pytest.mark.asyncio
async def test_tool_catalog_requires_authenticated_browser_user(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    catalog = _route(setup_skills_routes(SkillsManager(str(tmp_path))), "/api/skills/tool-options", "GET")
    with pytest.raises(HTTPException) as error:
        await catalog(_request(None))
    assert error.value.status_code == 401
