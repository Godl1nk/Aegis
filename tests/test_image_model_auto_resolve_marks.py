"""Image-model `auto` resolution must work on a local/mixed endpoint without
the user hand-configuring a default. Priority: explicit per-model marks →
whole-endpoint image type → auto-detect by model name (FLUX/ERNIE-Image/etc.).

Fix for repeated "no image model configured" failures on an endpoint that
clearly serves image models."""

import json
import types

import src.agent_tools  # noqa: F401


def _install_fake_db(monkeypatch, rows):
    """rows: list of (name, image_models, model_type, cached_models)."""
    class _Ep:
        def __init__(self, name, image_models, model_type, cached_models):
            self.name = name
            self.image_models = image_models
            self.model_type = model_type
            self.cached_models = cached_models
            self.is_enabled = True

    class _Query:
        def __init__(self, eps):
            self._eps = eps
        def filter(self, *a, **k):
            return self
        def all(self):
            return self._eps

    class _Session:
        def __init__(self, eps):
            self._eps = eps
        def query(self, *a, **k):
            return _Query(self._eps)
        def close(self):
            pass

    eps = [_Ep(*r) for r in rows]
    fake = types.SimpleNamespace(
        SessionLocal=lambda: _Session(eps),
        ModelEndpoint=types.SimpleNamespace(is_enabled=True),
    )
    monkeypatch.setitem(__import__("sys").modules, "src.database", fake)


def test_explicit_marks_win(monkeypatch):
    from src import ai_interaction
    _install_fake_db(monkeypatch, [
        ("chat", None, "llm", json.dumps(["Qwen3.6-27B"])),
        ("astrid", json.dumps(["ERNIE-Image", "FLUX.2-klein"]), "llm",
         json.dumps(["Qwen3.6-27B", "ERNIE-Image", "FLUX.2-klein"])),
    ])
    assert ai_interaction._first_marked_image_model(None) == "ERNIE-Image@astrid"


def test_whole_endpoint_image_type(monkeypatch):
    from src import ai_interaction
    _install_fake_db(monkeypatch, [
        ("chat", None, "llm", json.dumps(["Qwen3.6-27B"])),
        ("comfy", None, "image", json.dumps(["sdxl-base", "flux-dev"])),
    ])
    assert ai_interaction._first_marked_image_model(None) == "sdxl-base@comfy"


def test_name_autodetect_without_any_config(monkeypatch):
    """The robustness fix: no marks, no image-type endpoint — resolution still
    finds FLUX/ERNIE-Image purely by model name."""
    from src import ai_interaction
    _install_fake_db(monkeypatch, [
        ("astrid", None, "llm", json.dumps([
            "Qwen3.6-27B", "GRM-2.6-0628-IQ4_NL", "DeepResearch-30B-A3B",
            "ERNIE-Image", "FLUX.2-klein",
        ])),
    ])
    got = ai_interaction._first_marked_image_model(None)
    assert got in ("ERNIE-Image@astrid", "FLUX.2-klein@astrid"), got


def test_no_image_model_anywhere_returns_empty(monkeypatch):
    from src import ai_interaction
    _install_fake_db(monkeypatch, [
        ("chat", None, "llm", json.dumps(["Qwen3.6-27B", "llama-3-8b"])),
    ])
    assert ai_interaction._first_marked_image_model(None) == ""


def test_hallucinated_model_name_falls_back_to_real_image_model(monkeypatch):
    """Chat models invent image model names ("flux", "sdxl") they were never
    told exist. That guess must degrade to the configured/marked image model
    instead of hard-failing direct generation (the exact 'No endpoint found
    with image model flux' bug)."""
    from src import ai_interaction

    real = ("https://api.astrid/v1/chat/completions", "ERNIE-Image", {"k": "v"})

    def fake_resolve(spec, owner=None):
        if spec == "ERNIE-Image@astrid":
            return real
        raise ValueError(f"Model '{spec}' not found")

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(ai_interaction, "_first_marked_image_model", lambda owner=None: "ERNIE-Image@astrid")

    # Hallucinated name -> falls back to the marked model.
    assert ai_interaction._resolve_image_model_with_fallback("flux", None) == real
    # Valid name resolves directly (no fallback interference).
    assert ai_interaction._resolve_image_model_with_fallback("ERNIE-Image@astrid", None) == real


def test_fallback_raises_only_when_nothing_resolves(monkeypatch):
    from src import ai_interaction
    import pytest

    monkeypatch.setattr(
        ai_interaction, "_resolve_model",
        lambda spec, owner=None: (_ for _ in ()).throw(ValueError(spec)),
    )
    monkeypatch.setattr(ai_interaction, "_first_marked_image_model", lambda owner=None: "")
    with pytest.raises(ValueError):
        ai_interaction._resolve_image_model_with_fallback("flux", None)
