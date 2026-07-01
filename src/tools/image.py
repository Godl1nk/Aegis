"""Image-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the edit_image (gallery) tool.
``src.tool_implementations`` re-exports these for backward compatibility.
``_INTERNAL_BASE`` still lives in tool_implementations.py and is pulled back
function-locally here.
"""
from typing import Dict, Optional

from src.tools._common import _parse_tool_args


async def do_edit_image(content: str, owner: Optional[str] = None) -> Dict:
    """Edit a gallery image (upscale, rembg, inpaint, harmonize)."""
    import httpx
    from src.tool_implementations import _INTERNAL_BASE  # shared constant, still lives in the facade
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    image_id = args.get("image_id", "")
    action = args.get("action", "")
    if not image_id or not action:
        return {"error": "image_id and action are required", "exit_code": 1}
    payload = {"image_id": image_id}
    if args.get("prompt"):
        payload["prompt"] = args["prompt"]
    if args.get("scale"):
        payload["scale"] = args["scale"]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/gallery/{action}", json=payload)
            data = resp.json()
        if data.get("success") or data.get("id"):
            return {"output": f"Image edited ({action}). New image ID: {data.get('id', '?')}", "exit_code": 0}
        return {"error": data.get("error", f"{action} failed"), "exit_code": 1}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_ai_edit_image(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> Dict:
    """AI-powered image editing via img2img. Resolves a gallery image or
    uploaded attachment and sends it to the image generation model with a
    prompt describing changes."""
    from pathlib import Path
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    image_id = args.get("image_id", "")
    prompt = args.get("prompt", "")
    if not prompt:
        return {"error": "prompt is required", "exit_code": 1}

    # Resolve the image to a reference URL that do_generate_image can consume.
    # image_id may be: a gallery ID, an upload ID, or already a URL from
    # _collect_image_context (e.g. "/api/generated-image/..." or "upload:...").
    image_url = None

    if image_id:
        # Already a URL — use directly
        if image_id.startswith("/") or image_id.startswith("upload:") or image_id.startswith("data:"):
            image_url = image_id
        else:
            # 1) Gallery image (generated images stored in gallery_images table)
            try:
                from core.database import SessionLocal, GalleryImage
                from src.constants import GENERATED_IMAGES_DIR
                db = SessionLocal()
                try:
                    q = db.query(GalleryImage).filter(
                        GalleryImage.id == image_id,
                        GalleryImage.is_active == True,  # noqa: E712
                    )
                    if owner:
                        q = q.filter(GalleryImage.owner == owner)
                    img = q.first()
                    if img and img.filename:
                        path = Path(GENERATED_IMAGES_DIR) / img.filename
                        if path.is_file():
                            image_url = f"/api/generated-image/{img.filename}"
                finally:
                    db.close()
            except Exception:
                pass

            # 2) Uploaded attachment (user pasted/dropped an image)
            if not image_url:
                try:
                    from src.constants import BASE_DIR, UPLOAD_DIR
                    from src.upload_handler import UploadHandler
                    handler = UploadHandler(BASE_DIR, UPLOAD_DIR)
                    info = handler.resolve_upload(image_id, owner=owner)
                    if info and str(info.get("mime") or "").startswith("image/"):
                        path = Path(info["path"])
                        if path.is_file():
                            image_url = f"upload:{image_id}"
                except Exception:
                    pass

            # 3) Filename fallback — model may send original filename instead of upload ID
            if not image_url:
                try:
                    from src.constants import BASE_DIR, UPLOAD_DIR
                    from src.upload_handler import UploadHandler
                    handler = UploadHandler(BASE_DIR, UPLOAD_DIR)
                    for uid, info in handler._load_upload_index().items():
                        if isinstance(info, dict) and info.get("original_name") == image_id:
                            resolved = handler.resolve_upload(uid, owner=owner)
                            if resolved and str(resolved.get("mime") or "").startswith("image/"):
                                path = Path(resolved["path"])
                                if path.is_file():
                                    image_url = f"upload:{uid}"
                            break
                except Exception:
                    pass

    if not image_url:
        return {"error": "No image to edit. Upload an image or generate one first, then ask to edit it.", "exit_code": 1}

    model = args.get("model", "auto")
    size = args.get("size", "")
    denoising_strength = args.get("denoising_strength")
    if denoising_strength is None:
        denoising_strength = 0.65
    else:
        try:
            denoising_strength = float(denoising_strength)
        except (TypeError, ValueError):
            denoising_strength = 0.65
        denoising_strength = max(0.05, min(1.0, denoising_strength))

    lines = [prompt, model or "auto"]
    if size:
        lines.append(size)
    gen_content = "\n".join(lines)

    from src.ai_interaction import do_generate_image
    result = await do_generate_image(
        gen_content,
        session_id=session_id,
        owner=owner,
        reference_image_urls=[image_url],
        denoising_strength=denoising_strength,
    )
    if result.get("error"):
        return {"error": result["error"], "exit_code": 1}
    result_image_url = result.get("image_url")
    if result_image_url and result_image_url == image_url:
        return {
            "error": "Image edit backend returned the original reference image URL instead of a new edited image.",
            "exit_code": 1,
        }
    output_parts = []
    if result_image_url:
        output_parts.append(f"Edited image: {result_image_url}")
    if result.get("image_id"):
        output_parts.append(f"Image ID: {result['image_id']}")
    output_parts.append("Note: output was not visually inspected; do not claim specific changed values unless a vision tool verifies it.")
    return {
        "output": "\n".join(output_parts) or "Image edited",
        "image_url": result_image_url,
        "image_id": result.get("image_id"),
        "exit_code": 0,
    }
