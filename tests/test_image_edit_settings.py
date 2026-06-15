import asyncio
import json

import src.settings as settings_mod
from src.tool_implementations import do_manage_settings


def test_image_edit_model_in_default_settings():
    assert "image_edit_model" in settings_mod.DEFAULT_SETTINGS
    assert settings_mod.DEFAULT_SETTINGS["image_edit_model"] == ""


def test_image_edit_model_aliases(monkeypatch):
    store = {"image_edit_model": ""}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "save_settings", lambda s: store.update(s))

    # Test "image edit model" alias
    result1 = asyncio.run(do_manage_settings(json.dumps({
        "action": "set", "key": "image edit model", "value": "edit-model-1",
    })))
    assert result1.get("exit_code") == 0
    assert store.get("image_edit_model") == "edit-model-1"

    # Test "image editing model" alias
    result2 = asyncio.run(do_manage_settings(json.dumps({
        "action": "set", "key": "image editing model", "value": "edit-model-2",
    })))
    assert result2.get("exit_code") == 0
    assert store.get("image_edit_model") == "edit-model-2"
