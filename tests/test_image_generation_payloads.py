import base64
import io

import pytest
from PIL import Image

import src.ai_interaction as ai


def _png_b64(size=(8, 8)):
    img = Image.new("RGB", size, (20, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.asyncio
async def test_local_gpt_named_image_endpoint_gets_minimal_generation_payload(monkeypatch, tmp_path):
    captured = {}
    img_b64 = _png_b64((1200, 600))

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"b64_json": img_b64}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(ai, "GENERATED_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(ai, "_resolve_model", lambda *a, **k: (
        "http://127.0.0.1:8100/v1/chat/completions",
        "gpt-image-1",
        {},
    ))
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default="": default)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    result = await ai.do_generate_image(
        '{"prompt":"test","model":"local-gpt","size":"1280x720","quality":"high"}'
    )

    assert "error" not in result
    assert captured["url"] == "http://127.0.0.1:8100/v1/images/generations"
    assert captured["json"]["size"] == "1280x720"
    assert "quality" not in captured["json"]
    assert "steps" not in captured["json"]


@pytest.mark.asyncio
async def test_local_gpt_named_reference_image_uses_local_img2img(monkeypatch, tmp_path):
    captured = {}
    img_b64 = _png_b64((1024, 512))
    (tmp_path / "ref.png").write_bytes(base64.b64decode(img_b64))

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"image": img_b64}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, data=None, files=None, headers=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            captured["data"] = data
            captured["files"] = files
            return FakeResponse()

    monkeypatch.setattr(ai, "GENERATED_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(ai, "_resolve_model", lambda *a, **k: (
        "http://127.0.0.1:8100/v1/chat/completions",
        "gpt-image-1",
        {},
    ))
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default="": default)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    result = await ai.do_generate_image(
        '{"prompt":"edit","model":"local-gpt","quality":"high"}',
        reference_image_urls=["/api/generated-image/ref.png"],
    )

    assert "error" not in result
    assert captured["url"] == "http://127.0.0.1:8100/v1/images/img2img"
    assert captured["data"] is None
    assert captured["files"] is None
    assert captured["json"]["size"] == "1024x512"
    assert "quality" not in captured["json"]
    assert "steps" not in captured["json"]


@pytest.mark.asyncio
async def test_local_reference_image_a1111_fallback_gets_target_dimensions(monkeypatch, tmp_path):
    calls = []
    img_b64 = _png_b64()
    (tmp_path / "ref.png").write_bytes(base64.b64decode(img_b64))

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, **kwargs):
            calls.append((url, json))
            if len(calls) < 3:
                return FakeResponse(404, {"error": "missing"})
            return FakeResponse(200, {"images": [img_b64]})

    monkeypatch.setattr(ai, "GENERATED_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(ai, "_resolve_model", lambda *a, **k: (
        "http://127.0.0.1:8100/v1/chat/completions",
        "ernie-image",
        {},
    ))
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default="": default)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    result = await ai.do_generate_image(
        '{"prompt":"edit","model":"local","size":"768x512"}',
        reference_image_urls=["/api/generated-image/ref.png"],
    )

    assert "error" not in result
    assert calls[2][0] == "http://127.0.0.1:8100/sdapi/v1/img2img"
    assert calls[2][1]["width"] == 768
    assert calls[2][1]["height"] == 512
    assert "steps" not in calls[2][1]
