import json
from types import SimpleNamespace

import pytest

from src.tool_implementations import do_ai_edit_image


class _Field:
    def __eq__(self, other):
        return ("eq", other)


class _GalleryImage:
    id = _Field()
    owner = _Field()
    is_active = _Field()


class _Query:
    def __init__(self, image):
        self.image = image

    def filter(self, *args):
        return self

    def first(self):
        return self.image


class _Db:
    def __init__(self, image):
        self.image = image

    def query(self, model):
        return _Query(self.image)

    def close(self):
        pass


@pytest.mark.asyncio
async def test_ai_edit_image_resolves_gallery_id_from_generated_images_dir(monkeypatch, tmp_path):
    filename = "abc.png"
    (tmp_path / filename).write_bytes(b"png")
    captured = {}

    import core.database as db_mod
    import src.constants as constants

    monkeypatch.setattr(constants, "GENERATED_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(db_mod, "GalleryImage", _GalleryImage)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Db(SimpleNamespace(filename=filename)))

    async def fake_generate(content, **kwargs):
        captured["content"] = content
        captured.update(kwargs)
        return {"image_url": "/api/generated-image/edited.png", "image_id": "new-id"}

    monkeypatch.setattr("src.ai_interaction.do_generate_image", fake_generate)

    result = await do_ai_edit_image(json.dumps({
        "image_id": "550e8400-e29b-41d4-a716-446655440000",
        "prompt": "make it brighter",
    }))

    assert result["exit_code"] == 0
    assert captured["reference_image_urls"] == ["/api/generated-image/abc.png"]
    assert captured["denoising_strength"] == 0.65
