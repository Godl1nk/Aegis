import json
from src.ai_interaction import format_ideogram_prompt
from src.settings import get_setting, load_settings, save_settings

def test_format_ideogram_prompt():
    # 1. Test normal prompt formatting
    user_prompt = "a red apple on a wooden table, studio photo"
    size = "1024x1024"
    formatted = format_ideogram_prompt(user_prompt, size)

    # Verify it is valid JSON
    data = json.loads(formatted)
    assert isinstance(data, dict)
    assert data["high_level_description"] == "A red apple on a wooden table"
    assert data["style_description"]["medium"] == "photograph"
    assert "#B32020" in data["style_description"]["color_palette"]
    assert "Square 1024 by 1024 image" in data["compositional_deconstruction"]["canvas"]

    # Verify elements and dynamic bbox
    elements = data["compositional_deconstruction"]["elements"]
    assert len(elements) == 1
    assert elements[0]["type"] == "obj"
    assert elements[0]["bbox"] == [320, 320, 759, 719]
    assert elements[0]["desc"] == "A red apple on a wooden table"

    # Test landscape prompt which triggers full canvas bbox
    scenic_prompt = "a majestic mountain landscape under a starry sky"
    scenic_formatted = format_ideogram_prompt(scenic_prompt, size)
    scenic_data = json.loads(scenic_formatted)
    assert scenic_data["compositional_deconstruction"]["elements"][0]["bbox"] == [0, 0, 1024, 1024]

    # 2. Test already valid JSON prompt pass-through (no double wrapping)
    already_json = json.dumps({
        "high_level_description": "Custom high level description",
        "style_description": {
            "medium": "illustration"
        }
    })
    passthrough = format_ideogram_prompt(already_json, size)
    assert passthrough == already_json

def test_format_ideogram_prompt_other_mediums():
    # Test painting
    p_prompt = "watercolor painting of a blue cat"
    p_formatted = format_ideogram_prompt(p_prompt, "1024x768")
    p_data = json.loads(p_formatted)
    assert p_data["style_description"]["medium"] == "painting"
    assert p_data["style_description"]["aesthetics"] == "artistic, colorful, detailed, expressive"
    assert "Landscape 1024 by 768 image" in p_data["compositional_deconstruction"]["canvas"]
    assert "#2060B3" in p_data["style_description"]["color_palette"]
    # watercolor painting of a blue cat -> neither is_full_scene nor has_multiple is True
    # w=1024, h=768
    # bbox should be [320, 240, 759, 539]
    assert p_data["compositional_deconstruction"]["elements"][0]["bbox"] == [320, 240, 759, 539]

def test_format_ideogram_prompt_custom_template():
    custom_template = """{
        "my_custom_prompt": "{{clean_prompt}}",
        "custom_medium": "{{medium}}",
        "custom_palette": {{palette}}
    }"""
    # Temporarily set the setting
    settings = load_settings()
    original_template = settings.get("image_prompt_json_template")
    settings["image_prompt_json_template"] = custom_template
    save_settings(settings)

    try:
        formatted = format_ideogram_prompt("a red apple", "1024x1024")
        data = json.loads(formatted)
        assert data["my_custom_prompt"] == "A red apple"
        assert data["custom_medium"] == "photograph"
        assert "#B32020" in data["custom_palette"]
    finally:
        # Restore original template
        settings = load_settings()
        if original_template is not None:
            settings["image_prompt_json_template"] = original_template
        else:
            settings.pop("image_prompt_json_template", None)
        save_settings(settings)
