import asyncio
from types import SimpleNamespace

import pytest

import routes.task_routes as task_routes
import src.integrations as integrations
import src.task_action_policy as task_action_policy
from src.task_scheduler import TaskScheduler


def test_discord_task_output_posts_an_embed(monkeypatch):
    sent = {}

    monkeypatch.setattr(
        task_action_policy,
        "owner_has_admin_task_privileges",
        lambda owner: owner == "admin",
    )
    monkeypatch.setattr(
        integrations,
        "get_integration",
        lambda integration_id: {
            "id": integration_id,
            "preset": "discord_webhook",
            "enabled": True,
        },
    )

    async def fake_execute(integration_id, method, path, **kwargs):
        sent.update(
            integration_id=integration_id,
            method=method,
            path=path,
            body=kwargs["body"],
        )
        return {"exit_code": 0, "output": "HTTP 204"}

    monkeypatch.setattr(integrations, "execute_api_call", fake_execute)
    scheduler = TaskScheduler.__new__(TaskScheduler)
    task = SimpleNamespace(id="task-1", owner="admin", name="Market Brief")

    asyncio.run(
        scheduler._deliver_via_integration(
            "integration:discord-1",
            task,
            "S&P 500 futures rose.",
        )
    )

    assert sent == {
        "integration_id": "discord-1",
        "method": "POST",
        "path": "/",
        "body": {
            "embeds": [{
                "title": "Market Brief",
                "description": "S&P 500 futures rose.",
                "color": 5793266,
            }],
        },
    }


def test_discord_task_output_rejects_non_admin_owner(monkeypatch):
    monkeypatch.setattr(
        task_action_policy,
        "owner_has_admin_task_privileges",
        lambda owner: False,
    )
    scheduler = TaskScheduler.__new__(TaskScheduler)
    task = SimpleNamespace(id="task-1", owner="member", name="Market Brief")

    with pytest.raises(PermissionError, match="admin privileges"):
        asyncio.run(
            scheduler._deliver_via_integration(
                "integration:discord-1",
                task,
                "result",
            )
        )


def test_output_targets_include_enabled_discord_for_admin(monkeypatch):
    monkeypatch.setattr(task_routes, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(
        task_routes,
        "owner_has_admin_task_privileges",
        lambda owner: owner == "admin",
    )
    monkeypatch.setattr(
        integrations,
        "load_integrations",
        lambda: [
            {
                "id": "discord-1",
                "name": "Markets",
                "preset": "discord_webhook",
                "base_url": "https://discord.com/api/webhooks/example/token",
                "enabled": True,
            },
            {
                "id": "disabled",
                "name": "Disabled",
                "preset": "discord_webhook",
                "base_url": "https://discord.com/api/webhooks/example/disabled",
                "enabled": False,
            },
        ],
    )

    router = task_routes.setup_task_routes(None)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/tasks/meta/output-targets"
    )
    response = asyncio.run(endpoint(object()))

    discord_targets = [
        target for target in response["targets"]
        if target["value"].startswith("integration:")
    ]
    assert discord_targets == [{
        "value": "integration:discord-1",
        "label": "Discord → Markets",
        "description": "Send the completed task result to this Discord webhook",
    }]
