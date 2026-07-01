"""
image_gen_server.py

MCP server exposing image generation via OpenAI-compatible APIs.
"""

import asyncio
import base64
import hashlib
import sys
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import GENERATED_IMAGES_DIR

server = Server("image_gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image using an image-capable model (e.g. gpt-image-1)",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Model name (auto-detects if omitted or set to auto)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_image":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = arguments.get("prompt", "")
    model_spec = arguments.get("model", "")
    if isinstance(model_spec, str) and model_spec.strip().lower() == "auto":
        model_spec = ""
    size = arguments.get("size", "1024x1024")
    quality = arguments.get("quality", "medium")

    if not prompt:
        return [TextContent(type="text", text="Error: Image prompt is required")]

    try:
        import httpx
        from src.settings import load_settings, get_setting
        from src.ai_interaction import _resolve_model

        if not get_setting("image_gen_enabled", True):
            return [TextContent(type="text", text="Error: Image generation is disabled by the administrator.")]

        _settings = load_settings()

        if not model_spec:
            model_spec = _settings.get("image_model", "")
        if quality == "medium" and _settings.get("image_quality"):
            quality = _settings["image_quality"]

        # Auto-detect best available image model
        if not model_spec:
            for candidate in ("gpt-image-1.5", "gpt-image-1", "dall-e-3"):
                try:
                    await asyncio.to_thread(_resolve_model, candidate)
                    model_spec = candidate
                    break
                except ValueError:
                    continue
            if not model_spec:
                return [TextContent(type="text", text="Error: No image model found. Configure one in Admin.")]

        url, model_id, headers = await asyncio.to_thread(_resolve_model, model_spec)

        is_openai_api = "api.openai.com" in url
        is_gpt_image = "gpt-image" in model_id.lower()
        is_dalle = "dall-e" in model_id.lower()
        base_url = url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
        images_url = base_url + "/images/generations"

        valid_gpt_sizes = {"1024x1024", "1024x1536", "1536x1024", "auto"}
        valid_dalle3_sizes = {"1024x1024", "1024x1792", "1792x1024"}
        if is_openai_api and is_gpt_image and size not in valid_gpt_sizes:
            size = "1024x1024"
        elif is_openai_api and is_dalle and size not in valid_dalle3_sizes:
            size = "1024x1024"

        # Format prompt if the model uses JSON prompt format (e.g. Ideogram-4)
        from src.ai_interaction import format_ideogram_prompt
        prompt_format_setting = get_setting("image_prompt_format", "auto")
        if prompt_format_setting == "json":
            format_type = "json"
        elif prompt_format_setting == "string":
            format_type = "string"
        else: # auto
            format_type = "json" if "ideogram" in model_id.lower() else "string"

        if format_type == "json":
            prompt = format_ideogram_prompt(prompt, size)
            sys.stderr.write(f"Ideogram formatted prompt: {prompt}\n")
            sys.stderr.flush()

        payload = {"model": model_id, "prompt": prompt, "n": 1, "size": size}
        if is_openai_api and is_gpt_image:
            if quality in ("low", "medium", "high", "auto"):
                payload["quality"] = quality
            else:
                payload["quality"] = "medium"
                quality = "medium"

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=900.0, write=30.0, pool=30.0)) as client:
            resp = await client.post(images_url, json=payload, headers=headers)

            if resp.status_code != 200:
                error_text = resp.text[:500]
                try:
                    err_json = resp.json()
                    error_text = err_json.get("error", {}).get("message", error_text) if isinstance(err_json.get("error"), dict) else str(err_json.get("error", error_text))
                except Exception:
                    pass
                if not error_text:
                    error_text = "empty response body"
                return [TextContent(type="text", text=f"Error: Image generation failed ({resp.status_code}) for {model_id} at {images_url}: {error_text}")]

            data = resp.json()
            images = data.get("data", [])
            if not images:
                return [TextContent(type="text", text="Error: No images returned from API")]

            img = images[0]
            image_url = None
            # Prefix the instance's public base URL (existing app_public_url setting) so the
            # link is fully-qualified and clickable when the model echoes it. Empty = relative
            # same-origin path (unchanged default).
            _pub_base = (get_setting("app_public_url", "") or "").rstrip("/")

            if img.get("b64_json"):
                img_dir = Path(GENERATED_IMAGES_DIR)
                img_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{uuid.uuid4().hex[:12]}.png"
                img_path = img_dir / filename
                img_path.write_bytes(base64.b64decode(img["b64_json"]))
                image_url = f"{_pub_base}/api/generated-image/{filename}"

                # Save to gallery, but make repeated identical backend results
                # idempotent so the library doesn't show duplicate tiles.
                image_id = str(uuid.uuid4())
                try:
                    from src.database import SessionLocal, GalleryImage
                    db = SessionLocal()
                    content = img_path.read_bytes()
                    file_hash = hashlib.sha256(content).hexdigest()
                    existing = db.query(GalleryImage).filter(
                        GalleryImage.filename == filename,
                        GalleryImage.is_active == True,  # noqa: E712
                    ).first()
                    if existing:
                        image_id = existing.id
                    else:
                        duplicate = db.query(GalleryImage).filter(
                            GalleryImage.file_hash == file_hash,
                            GalleryImage.is_active == True,  # noqa: E712
                            GalleryImage.owner == None,  # noqa: E711
                        ).first()
                        if duplicate:
                            if duplicate.filename != filename:
                                img_path.unlink(missing_ok=True)
                            filename = duplicate.filename
                            image_url = f"{_pub_base}/api/generated-image/{filename}"
                            image_id = duplicate.id
                        else:
                            db.add(GalleryImage(
                                id=image_id,
                                filename=filename,
                                prompt=prompt,
                                model=model_id,
                                size=size,
                                quality=quality,
                                file_hash=file_hash,
                                file_size=len(content),
                            ))
                            db.commit()
                except Exception:
                    image_id = None
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass

            elif img.get("url"):
                image_url = img["url"]
                image_id = None
            else:
                return [TextContent(type="text", text="Error: Unexpected image API response format")]

            # "Direct link:" rather than an "image_url:" label — small models copied the
            # label token ("image_url") into the link href, producing a broken link.
            result = (
                f"Generated image for: {prompt[:100]}\n"
                f"Direct link: {image_url}\n"
                f"model: {model_id}\nsize: {size}"
            )
            if image_id:
                result += f"\nimage_id: {image_id}"
            return [TextContent(type="text", text=result)]

    except httpx.TimeoutException:
        return [TextContent(type="text", text="Error: Image generation timed out (900s)")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
