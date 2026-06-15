import base64
import io
from types import SimpleNamespace
from PIL import Image
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

# Import app and globals from diffusion_server
import scripts.diffusion_server as ds


def test_diffusion_server_generation_uses_pipeline_default_steps(monkeypatch):
    img = Image.new("RGB", (64, 64), (0, 255, 0))

    class StableDiffusionPipeline:
        def __init__(self):
            self.calls = []

        def __call__(self, **kwargs):
            self.calls.append(kwargs)
            result = MagicMock()
            result.images = [img]
            return result

    pipe = StableDiffusionPipeline()
    ds._pipe = pipe
    ds._args = SimpleNamespace(width=1024, height=1024)

    client = TestClient(ds.app, base_url="http://127.0.0.1")
    response = client.post("/v1/images/generations", json={
        "prompt": "test image",
        "size": "512x512",
        "quality": "high",
        "n": 0,
    })

    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert pipe.calls
    assert "num_inference_steps" not in pipe.calls[0]
    assert pipe.calls[0]["width"] == 512
    assert pipe.calls[0]["height"] == 512

def test_diffusion_server_img2img(monkeypatch):
    # Create a small dummy image in base64
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    # Mock the pipeline and arguments in diffusion_server
    mock_result = MagicMock()
    mock_result.images = [Image.new("RGB", (100, 100), (0, 255, 0))]
    
    mock_pipe = MagicMock()
    mock_pipe.__class__.__name__ = "StableDiffusionPipeline"
    mock_pipe.return_value = mock_result
    
    ds._pipe = mock_pipe
    ds._args = MagicMock()
    ds._args.steps = 10
    
    # We mock _get_inpaint_pipe to return None, None to force the fallback to _pipe
    # We also mock _img2img_pipe to None
    monkeypatch.setattr(ds, "_get_inpaint_pipe", lambda: (None, None))
    ds._img2img_pipe = None
    
    client = TestClient(ds.app, base_url="http://127.0.0.1")
    
    payload = {
        "image": img_b64,
        "prompt": "a beautiful landscape",
        "model": "my-mock-model",
        "size": "512x512",
        "strength": 0.8,
        "steps": 15
    }
    
    response = client.post("/v1/images/img2img", json=payload)
    assert response.status_code == 200
    res_data = response.json()
    assert "image" in res_data
    assert "data" in res_data
    assert len(res_data["data"]) == 1
    assert "b64_json" in res_data["data"][0]

def test_diffusion_server_variations(monkeypatch):
    # Create a small dummy image in base64
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    # Mock the pipeline and arguments in diffusion_server
    mock_result = MagicMock()
    mock_result.images = [Image.new("RGB", (100, 100), (0, 255, 0))]
    
    mock_pipe = MagicMock()
    mock_pipe.__class__.__name__ = "StableDiffusionPipeline"
    mock_pipe.return_value = mock_result
    
    ds._pipe = mock_pipe
    ds._args = MagicMock()
    ds._args.steps = 10
    
    monkeypatch.setattr(ds, "_get_inpaint_pipe", lambda: (None, None))
    ds._img2img_pipe = None
    
    client = TestClient(ds.app, base_url="http://127.0.0.1")
    
    payload = {
        "image": img_b64,
        "prompt": "some variations",
        "size": "512x512"
    }
    
    response = client.post("/v1/images/variations", json=payload)
    assert response.status_code == 200
    res_data = response.json()
    assert "image" in res_data
    assert "data" in res_data
