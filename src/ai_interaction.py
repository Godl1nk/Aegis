"""
ai_interaction.py

AI-to-AI interaction tools: pipeline and manage_memory, plus shared model
resolution (_resolve_model), the session-manager singleton, and dispatch_ai_tool.

As part of the tool -> registry migration (#3629), chat_with_model, ask_teacher
and list_models moved to src/agent_tools/model_interaction_tools.py, and
create_session, list_sessions, send_to_session and manage_session moved to
src/agent_tools/session_tools.py. Those modules reuse get_session_manager /
_resolve_model / AI_CHAT_TIMEOUT from here.

These are agent tools — the LLM writes fenced code blocks and they execute
through the standard agent_tools.py pipeline.
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import uuid
import time
from typing import Dict, Optional, Tuple

from src.constants import GENERATED_IMAGES_DIR, IMAGE_EDIT_MAX_SIDE

logger = logging.getLogger(__name__)

AI_CHAT_TIMEOUT = 120  # seconds for a single LLM call
MAX_DEBATE_ROUNDS = 5
MAX_PIPELINE_STEPS = 10

# ---------------------------------------------------------------------------
# Global managers (set from app.py, same pattern as _mcp_manager)
# _session_manager is kept as a local cache for performance (avoiding
# repeated get_session_manager_instance() calls). It's synced with
# the authoritative singleton in core.models.
_session_manager = None
_memory_manager = None
_memory_vector = None
_rag_manager = None
_personal_docs_manager = None


def set_session_manager(mgr):
    """Set the global session manager. Syncs local cache + core singleton."""
    global _session_manager
    _session_manager = mgr
    from core.models import set_session_manager_instance
    set_session_manager_instance(mgr)


def get_session_manager():
    """Get the global session manager."""
    return _session_manager


def set_memory_manager(mgr, vector=None):
    global _memory_manager, _memory_vector
    _memory_manager = mgr
    _memory_vector = vector


def set_rag_manager(rag_mgr, personal_docs_mgr=None):
    global _rag_manager, _personal_docs_manager
    _rag_manager = rag_mgr
    _personal_docs_manager = personal_docs_mgr


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

from src.endpoint_resolver import build_chat_url, build_headers, build_models_url, resolve_endpoint_runtime


def _resolve_model(spec: str, owner: Optional[str] = None) -> Tuple[str, str, Dict]:
    """Resolve a model specifier to (endpoint_url, model_id, headers).

    Accepts:
      "model_name"              — searches all configured endpoints
      "model_name@endpoint_name" — looks up specific endpoint by display name

    Raises ValueError if model not found.
    """
    import httpx
    from src.database import SessionLocal, ModelEndpoint
    from src.llm_core import _detect_provider, ANTHROPIC_MODELS
    from src.auth_helpers import owner_filter

    spec = spec.strip()
    target_endpoint_name = None

    if "@" in spec:
        model_name, target_endpoint_name = spec.rsplit("@", 1)
        model_name = model_name.strip()
        target_endpoint_name = target_endpoint_name.strip()
    else:
        model_name = spec

    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if target_endpoint_name:
            query = query.filter(ModelEndpoint.name.ilike(f"%{target_endpoint_name}%"))
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoints = query.all()

        if not endpoints:
            raise ValueError("No enabled endpoints found" +
                             (f" matching '{target_endpoint_name}'" if target_endpoint_name else ""))

        for ep in endpoints:
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception:
                continue
            provider = _detect_provider(base)
            headers = build_headers(api_key, base)

            if provider == "anthropic":
                # Anthropic: match against hardcoded model list
                matched = None
                for am in ANTHROPIC_MODELS:
                    if model_name.lower() in am.lower() or am.lower() in model_name.lower():
                        matched = am
                        break
                if matched:
                    return build_chat_url(base), matched, headers
            else:
                # OpenAI-compatible and native Ollama: probe the provider's model list.
                try:
                    models_url = build_models_url(base)
                    if models_url:
                        r = httpx.get(models_url, headers=headers, timeout=5)
                        r.raise_for_status()
                        data = r.json()
                        items = data if isinstance(data, list) else (data.get("data") or [])
                        model_ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
                        if not model_ids:
                            model_ids = [
                                m.get("name") or m.get("model")
                                for m in (data.get("models") or [])
                                if m.get("name") or m.get("model")
                            ]
                    else:
                        model_ids = json.loads(ep.cached_models or "[]")
                except Exception:
                    model_ids = []

                # Exact match first
                for mid in model_ids:
                    if mid.lower() == model_name.lower():
                        return build_chat_url(base), mid, headers

                # Partial match
                for mid in model_ids:
                    if model_name.lower() in mid.lower() or mid.lower() in model_name.lower():
                        return build_chat_url(base), mid, headers

        raise ValueError(f"Model '{spec}' not found on any configured endpoint")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------



async def stream_ai_tool(tool: str, content: str, session_id: Optional[str] = None, owner: Optional[str] = None):
    """Dispatcher for streaming AI tools. Yields events as async generator."""
    # Fallback: run non-streaming and yield final result
    desc, result = await dispatch_ai_tool(tool, content, session_id, owner=owner)
    yield {"_final": True, "desc": desc, "result": result}


async def do_pipeline(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Execute a multi-step pipeline where each model's output feeds the next.

    Content format (JSON):
      {"steps": [
        {"model": "model_a", "instruction": "Draft an essay about X"},
        {"model": "model_b", "instruction": "Critique the following draft"},
        {"model": "model_a", "instruction": "Revise based on this critique"}
      ]}

    Or line format:
      Line 1: step1_model | step1_instruction
      Line 2: step2_model | step2_instruction
      ...
    """
    from src.llm_core import llm_call_async

    # Try JSON parse first
    steps = None
    try:
        data = json.loads(content.strip())
        if isinstance(data, dict) and "steps" in data:
            steps = data["steps"]
        elif isinstance(data, list):
            steps = data
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to line format: model | instruction
    if not steps:
        steps = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                parts = line.split("|", 1)
                steps.append({"model": parts[0].strip(), "instruction": parts[1].strip()})
            else:
                return {"error": "Each line must be: model | instruction (or use JSON format)"}

    if not steps:
        return {"error": "No pipeline steps provided"}
    if len(steps) > MAX_PIPELINE_STEPS:
        return {"error": f"Maximum {MAX_PIPELINE_STEPS} steps allowed"}

    # Resolve all models first (fail fast)
    resolved = []
    for i, step in enumerate(steps):
        model_spec = step.get("model", "").strip()
        instruction = step.get("instruction", "").strip()
        if not model_spec or not instruction:
            return {"error": f"Step {i + 1}: both 'model' and 'instruction' are required"}
        try:
            url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
            resolved.append((url, model, headers, instruction))
        except ValueError as e:
            return {"error": f"Step {i + 1}: {e}"}

    # Execute pipeline
    step_outputs = []
    previous_output = None

    try:
        for i, (url, model, headers, instruction) in enumerate(resolved):
            if previous_output:
                user_content = (
                    f"Previous step's output:\n\n{previous_output}\n\n"
                    f"Your task: {instruction}"
                )
            else:
                user_content = instruction

            messages = [
                {"role": "system", "content": f"You are step {i + 1} in a processing pipeline. {instruction}"},
                {"role": "user", "content": user_content},
            ]

            response = await llm_call_async(
                url, model, messages, headers=headers, timeout=AI_CHAT_TIMEOUT
            )

            step_outputs.append({
                "step": i + 1,
                "model": model,
                "instruction": instruction,
                "output": response[:5000] if len(response) > 5000 else response,
            })

            previous_output = response

        # Build readable result
        result_lines = [f"# Pipeline Results ({len(resolved)} steps)\n"]
        for so in step_outputs:
            result_lines.append(f"## Step {so['step']}: {so['model']}")
            result_lines.append(f"*Instruction: {so['instruction']}*\n")
            result_lines.append(so["output"])
            result_lines.append("\n---\n")

        return {
            "results": "\n".join(result_lines),
            "steps": step_outputs,
            "final_output": previous_output,
        }
    except Exception as e:
        logger.error(f"pipeline failed at step {len(step_outputs) + 1}: {e}")
        return {"error": f"Pipeline failed at step {len(step_outputs) + 1}: {e}"}


# ---------------------------------------------------------------------------
# Session management tool
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Memory management tool
# ---------------------------------------------------------------------------

def _norm_memory_text(s: str) -> str:
    """Lowercase + collapse whitespace + trim edge punctuation for comparison."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip(" .,!?:;\"'`")


def _find_duplicate_memory(text, existing, memory_manager, memory_vector, owner):
    """Return an existing memory that duplicates ``text``, or None.

    Mirrors the auto-extractor's dedup so the agent's ``manage_memory add``
    can't create near-duplicates (the write path previously appended
    unconditionally). Checks, strongest first:

      1. exact text match,
      2. vector semantic match (cosine ≥ 0.85) — off when ChromaDB is down,
      3. fuzzy Jaccard at the extractor's 0.75 threshold (deliberately high so
         opposite facts that merely share tokens are NOT merged),
      4. superset/prefix containment — catches "name is X" vs "name is X + more"
         even with the vector store down; safe because opposite facts diverge
         rather than nest.
    """
    nt = _norm_memory_text(text)
    if not nt:
        return None

    # 1. exact
    for e in existing:
        if _norm_memory_text(e.get("text", "")) == nt:
            return e

    # 2. vector semantic match (strongest; requires a healthy store)
    if memory_vector and getattr(memory_vector, "healthy", False):
        try:
            try:
                sim_id = memory_vector.find_similar(text, threshold=0.85, owner=owner)
            except TypeError:
                sim_id = memory_vector.find_similar(text, threshold=0.85)
        except Exception:
            sim_id = None
        if sim_id:
            match = next((e for e in existing if e.get("id") == sim_id), None)
            if match is not None:
                return match

    # 3. fuzzy text (same 0.75 threshold as the extractor)
    try:
        from services.memory.memory_extractor import _is_text_duplicate
        if _is_text_duplicate(text, existing):
            new_tokens = set(nt.split())
            best, best_score = None, 0.0
            for e in existing:
                ot = set(_norm_memory_text(e.get("text", "")).split())
                if not ot:
                    continue
                j = len(new_tokens & ot) / len(new_tokens | ot)
                if j > best_score:
                    best, best_score = e, j
            if best is not None:
                return best
    except Exception:
        logger.debug("fuzzy memory dedup unavailable", exc_info=True)

    # 4. superset / prefix containment (≥3-token shorter side, word-boundaried)
    for e in existing:
        ne = _norm_memory_text(e.get("text", ""))
        if not ne:
            continue
        shorter, longer = (nt, ne) if len(nt) <= len(ne) else (ne, nt)
        if len(shorter.split()) >= 3 and (longer == shorter or longer.startswith(shorter + " ")):
            return e

    return None


async def do_manage_memory(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Manage memories: list, add, edit, delete, search.

    Content format:
      Line 1: action (list|add|edit|delete|search)
      Line 2+: action-specific params

    Actions:
      list                    — list all memories (optional line 2: category filter)
      add                     — line 2: text, optional line 3: category (fact|event|contact|preference)
      edit                    — line 2: memory_id, line 3: new text
      delete                  — line 2: memory_id
      search                  — line 2: query
    """
    if not _memory_manager:
        return {"error": "Memory manager not available"}

    lines = content.strip().split("\n")
    if not lines:
        return {"error": "Need at least 1 line: action"}

    action = lines[0].strip().lower()

    if action == "list":
        category_filter = lines[1].strip().lower() if len(lines) > 1 and lines[1].strip() else None
        memories = _memory_manager.load(owner=owner)
        if category_filter:
            memories = [m for m in memories if m.get("category", "").lower() == category_filter]
        if not memories:
            return {"results": "No memories found" + (f" in category '{category_filter}'" if category_filter else "") + "."}

        result_lines = [f"Found {len(memories)} memory entries:\n"]
        for m in memories:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            text = m.get("text", "")
            if len(text) > 150:
                text = text[:150] + "..."
            result_lines.append(f"- [{cat}] `{mid}` — {text}")
        return {"results": "\n".join(result_lines)}

    elif action == "add":
        if len(lines) < 2:
            return {"error": "Add needs line 2: memory text"}
        text = lines[1].strip()
        category = lines[2].strip().lower() if len(lines) > 2 and lines[2].strip() else "fact"
        if not text:
            return {"error": "Memory text cannot be empty"}

        memories = _memory_manager.load_all()
        # Dedup before creating. The agent add path used to append
        # unconditionally, so the model produced near-duplicate memories
        # (e.g. "User's name is X" then "User's name is X, studying at Y").
        # Mirror the auto-extractor's dedup: exact → vector → fuzzy → superset.
        _user_mem = [
            m for m in memories
            if (not owner) or m.get("owner") == owner or m.get("owner") is None
        ]
        _dup = _find_duplicate_memory(text, _user_mem, _memory_manager, _memory_vector, owner)
        if _dup is not None:
            return {
                "action": "add",
                "memory_id": _dup["id"],
                "duplicate": True,
                "results": (
                    f"Already remembered (id {str(_dup['id'])[:8]}): "
                    f"{_dup.get('text', '')}. Not adding a duplicate — use the "
                    f"edit action on that id if you need to refine or extend it."
                ),
            }

        entry = _memory_manager.add_entry(text, source="ai_agent", category=category, owner=owner)
        memories.append(entry)
        _memory_manager.save(memories)

        # Update vector index if available
        if _memory_vector and hasattr(_memory_vector, 'healthy') and _memory_vector.healthy:
            try:
                try:
                    _memory_vector.add(entry["id"], text, owner=owner, kind=entry.get("kind"))
                except TypeError:
                    _memory_vector.add(entry["id"], text)
            except Exception:
                pass
        try:
            from src.event_bus import fire_event
            fire_event("memory_added", owner)
        except Exception:
            logger.debug("memory_added event dispatch failed", exc_info=True)

        return {"action": "add", "memory_id": entry["id"],
                "results": f"Memory added: [{category}] {text}"}

    elif action == "edit":
        if len(lines) < 3:
            return {"error": "Edit needs line 2: memory_id, line 3: new text"}
        memory_id = lines[1].strip()
        new_text = lines[2].strip()
        if not new_text:
            return {"error": "New text cannot be empty"}

        memories = _memory_manager.load_all()
        found = False
        for m in memories:
            if m.get("id", "").startswith(memory_id):
                # Verify ownership
                if owner and m.get("owner") != owner:
                    return {"error": f"Memory '{memory_id}' not found"}
                m["text"] = new_text
                m["timestamp"] = int(time.time())
                found = True
                full_id = m["id"]
                break
        if not found:
            return {"error": f"Memory '{memory_id}' not found"}
        _memory_manager.save(memories)

        # Update vector index
        if _memory_vector and hasattr(_memory_vector, 'healthy') and _memory_vector.healthy:
            try:
                _memory_vector.remove(full_id)
                try:
                    _memory_vector.add(full_id, new_text, owner=owner, kind=m.get("kind"))
                except TypeError:
                    _memory_vector.add(full_id, new_text)
            except Exception:
                pass

        return {"action": "edit", "memory_id": memory_id,
                "results": f"Memory updated: {new_text}"}

    elif action == "delete":
        if len(lines) < 2:
            return {"error": "Delete needs line 2: memory_id"}
        memory_id = lines[1].strip()

        memories = _memory_manager.load_all()
        original_len = len(memories)
        full_id = None
        delete_id = None
        for m in memories:
            if m.get("id", "").startswith(memory_id):
                # Verify ownership
                if owner and m.get("owner") != owner:
                    return {"error": f"Memory '{memory_id}' not found"}
                full_id = m["id"]
                delete_id = m["id"]
                break
        if len([m for m in memories if m.get("id") != delete_id]) == original_len:
            return {"error": f"Memory '{memory_id}' not found"}
        if hasattr(_memory_manager, "delete_entry"):
            if not _memory_manager.delete_entry(delete_id, owner=owner):
                return {"error": f"Memory '{memory_id}' not found"}
        else:
            memories = [m for m in memories if m.get("id") != delete_id]
            _memory_manager.save(memories)

        # Remove from vector index
        if _memory_vector and full_id and hasattr(_memory_vector, 'healthy') and _memory_vector.healthy:
            try:
                _memory_vector.remove(full_id)
            except Exception:
                pass

        return {"action": "delete", "memory_id": memory_id,
                "results": f"Memory '{memory_id}' deleted"}

    elif action == "search":
        if len(lines) < 2:
            return {"error": "Search needs line 2: query"}
        query = lines[1].strip()
        memories = _memory_manager.load(owner=owner)
        query_lower = query.lower()
        exact_results = [m for m in memories if query_lower in (m.get("text", "").lower())]

        if hasattr(_memory_manager, 'get_relevant_memories'):
            vector_results = _memory_manager.get_relevant_memories(query, memories, threshold=0.05, max_items=20)
        else:
            vector_results = []
        seen = set()
        results = []
        for m in [*exact_results, *vector_results]:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            results.append(m)
            if len(results) >= 20:
                break

        if not results:
            return {"results": f"No memories found matching '{query}'."}
        result_lines = [f"Found {len(results)} matching memories:\n"]
        for m in results:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            text = m.get("text", "")
            result_lines.append(f"- [{cat}] `{mid}` — {text}")
        return {"results": "\n".join(result_lines)}

    else:
        return {"error": f"Unknown action '{action}'. Use: list, add, edit, delete, search"}


# ---------------------------------------------------------------------------
# RAG management tool
# ---------------------------------------------------------------------------

async def do_manage_rag(content: str, session_id: Optional[str] = None) -> Dict:
    """Manage RAG indexed documents: list, add_directory, remove_directory.

    Content format:
      Line 1: action (list|add_directory|remove_directory)
      Line 2: directory path (for add/remove)
    """
    lines = content.strip().split("\n")
    if not lines:
        return {"error": "No action specified"}
    action = lines[0].strip().lower()

    if action == "list":
        if not _personal_docs_manager:
            return {"results": "Personal docs manager not available. RAG may not be configured."}
        try:
            files = []
            if hasattr(_personal_docs_manager, 'index'):
                files = _personal_docs_manager.index or []
            dirs = []
            if hasattr(_personal_docs_manager, 'get_indexed_directories'):
                dirs = _personal_docs_manager.get_indexed_directories()

            result_lines = []
            if dirs:
                result_lines.append(f"**Indexed directories ({len(dirs)}):**")
                for d in dirs:
                    result_lines.append(f"  - `{d}`")
            if files:
                result_lines.append(f"\n**Indexed files ({len(files)}):**")
                for f in files[:50]:
                    name = f.get("name", str(f)) if isinstance(f, dict) else str(f)
                    result_lines.append(f"  - {name}")
                if len(files) > 50:
                    result_lines.append(f"  ... and {len(files) - 50} more")

            if not result_lines:
                return {"results": "No files or directories indexed in RAG."}
            return {"results": "\n".join(result_lines)}
        except Exception as e:
            return {"error": str(e)}

    elif action == "add_directory":
        if len(lines) < 2:
            return {"error": "add_directory needs line 2: directory path"}
        directory = lines[1].strip()

        import os
        directory = os.path.expanduser(directory)
        if not os.path.isdir(directory):
            return {"error": f"Directory not found: {directory}"}

        if not _rag_manager:
            return {"error": "RAG manager not available"}

        try:
            result = _rag_manager.index_personal_documents(directory)
            indexed = result.get("indexed", 0) if isinstance(result, dict) else 0
            return {"action": "add_directory", "directory": directory,
                    "results": f"Directory '{directory}' added to RAG index ({indexed} files indexed)"}
        except Exception as e:
            return {"error": f"Failed to index directory: {e}"}

    elif action == "remove_directory":
        if len(lines) < 2:
            return {"error": "remove_directory needs line 2: directory path"}
        directory = lines[1].strip()

        if not _personal_docs_manager:
            return {"error": "Personal docs manager not available"}

        try:
            if hasattr(_personal_docs_manager, 'remove_directory'):
                # Performs a targeted per-directory delete (#1660). The previous
                # unconditional _rag_manager.rebuild_index() here wiped the whole
                # collection on every remove (even for untracked dirs) and has
                # been removed.
                _personal_docs_manager.remove_directory(directory)
            return {"action": "remove_directory", "directory": directory,
                    "results": f"Directory '{directory}' removed from RAG index"}
        except Exception as e:
            return {"error": f"Failed to remove directory: {e}"}

    else:
        return {"error": f"Unknown action '{action}'. Use: list, add_directory, remove_directory"}


# ---------------------------------------------------------------------------
# UI control tool (returns events for frontend to apply)
# ---------------------------------------------------------------------------

async def do_ui_control(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """Control frontend UI: toggle settings, switch model, change theme.

    Content format:
      Line 1: action
      Line 2+: action-specific params

    Actions:
      toggle <name> <on|off>  — Toggle a setting (web, bash, rag, research, incognito, document_editor)
      set_mode <agent|chat>   — Switch between agent and chat mode
      switch_model <model>    — Change the model for the current session
      set_theme <preset>      — Apply a built-in theme preset (dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute)
      create_theme <name> <bg> <fg> <panel> <border> <accent> [key=val ...] — Create custom theme. Optional key=val: advanced color overrides AND background effects: bgPattern=<none|dots|synapse|rain|constellations|perlin-flow|petals|sparkles|embers>, bgEffectColor=#RRGGBB, bgEffectIntensity=<num>, bgEffectSize=<num>, frosted=true|false
      open_panel <name>       — Open a panel (documents, gallery, email, sessions, notes, memories, skills, journey, settings, cookbook)
      open_email_reply <uid> [folder] [reply|reply-all|ai-reply] [body text] — Open a reply draft document for an email; does not send. ALWAYS append the body text when the user told you what to say (one-shot draft); only omit body when the user just asked to "open a reply" without content.
      get_toggles             — Return current toggle states (server-side knowledge)
    """
    lines = content.strip().split("\n")
    if not lines:
        return {"error": "No action specified"}

    parts = lines[0].strip().split(None, 2)
    action = parts[0].lower()

    if action == "toggle":
        if len(parts) < 3:
            return {"error": "toggle needs: toggle <name> <on|off>"}
        toggle_name = parts[1].lower()
        state = parts[2].lower() in ("on", "true", "1", "yes", "enable", "enabled")
        # Friendly aliases — users say "shell" / "search" naturally.
        _toggle_aliases = {
            "shell": "bash",
            "terminal": "bash",
            "search": "web",
            "websearch": "web",
            "web_search": "web",
            "deepresearch": "research",
            "deep_research": "research",
            "documents": "document_editor",
            "doc": "document_editor",
            "docs": "document_editor",
            "private": "incognito",
        }
        toggle_name = _toggle_aliases.get(toggle_name, toggle_name)
        valid_toggles = {"web", "bash", "rag", "research", "incognito", "document_editor"}
        if toggle_name not in valid_toggles:
            return {"error": f"Unknown toggle '{toggle_name}'. Valid: {', '.join(sorted(valid_toggles))}"}
        return {
            "ui_event": "toggle",
            "toggle_name": toggle_name,
            "state": state,
            "results": f"Toggle '{toggle_name}' set to {'on' if state else 'off'}",
        }

    elif action == "set_mode":
        if len(parts) < 2:
            return {"error": "set_mode needs: set_mode <agent|chat>"}
        mode = parts[1].lower()
        if mode not in ("agent", "chat"):
            return {"error": f"Invalid mode '{mode}'. Use: agent, chat"}
        return {
            "ui_event": "set_mode",
            "mode": mode,
            "results": f"Mode changed to '{mode}'",
        }

    elif action == "switch_model":
        model_spec = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not model_spec:
            model_spec = lines[1].strip() if len(lines) > 1 else ""
        if not model_spec:
            return {"error": "switch_model needs a model name"}

        # Resolve the model to validate it exists
        try:
            url, model_id, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
        except ValueError as e:
            return {"error": str(e)}

        # Update current session's model if we have a session
        if session_id and _session_manager:
            from src.database import SessionLocal as SL2, Session as DbSess2
            db2 = SL2()
            try:
                db_s = db2.query(DbSess2).filter(DbSess2.id == session_id).first()
                if db_s:
                    db_s.endpoint_url = url
                    db_s.model = model_id
                    db2.commit()
            finally:
                db2.close()

            sess = _session_manager.get_session(session_id)
            if sess:
                sess.endpoint_url = url
                sess.model = model_id
                if headers:
                    sess.headers = headers

        return {
            "ui_event": "switch_model",
            "model": model_id,
            "endpoint_url": url,
            "results": f"Model switched to '{model_id}'",
        }

    elif action == "set_theme":
        theme_name = parts[1].lower() if len(parts) > 1 else ""
        # Theme colors are defined in static/js/theme.js on the frontend.
        # We pass the name; the frontend looks it up from presets + custom themes.
        # Also check user's custom themes stored in prefs.
        # Must match the THEMES keys in static/js/theme.js.
        known_presets = [
            "dark", "light", "midnight", "paper", "cyberpunk", "retrowave",
            "forest", "ocean", "ume", "copper", "terminal", "organs",
            "lavender", "gpt", "claude", "cute",
        ]
        custom_themes = {}
        try:
            from routes.prefs_routes import _load as _load_prefs
            custom_themes = _load_prefs().get("custom-themes", {}) or {}
        except Exception:
            pass
        all_known = set(known_presets) | set(custom_themes.keys())
        if theme_name not in all_known:
            custom_label = f" | Custom: {', '.join(sorted(custom_themes.keys()))}" if custom_themes else ""
            return {"error": f"Unknown theme '{theme_name}'. Available: {', '.join(sorted(known_presets))}{custom_label}"}
        return {
            "ui_event": "set_theme",
            "theme_name": theme_name,
            "results": f"Theme changed to '{theme_name}'",
        }

    elif action == "create_theme":
        # Re-split without limit to get all parts
        parts = lines[0].strip().split()
        # create_theme <name> <bg> <fg> <panel> <border> <accent> [key=value ...]
        if len(parts) < 7:
            return {"error": "create_theme needs: create_theme <name> <bg> <fg> <panel> <border> <accent> (all hex colors). Optional advanced color key=value pairs (userBubbleBg, aiBubbleBg, bubbleBorder, sidebarBg, sectionAccent, brandColor, inputBg, inputBorder, sendBtnBg, sendBtnHover, codeBg, codeFg, toggleBg, toggleActive, accentPrimary, accentError). Optional background EFFECTS: bgPattern=<none|dots|synapse|rain|constellations|perlin-flow|petals|sparkles|embers>, bgEffectColor=#RRGGBB, bgEffectIntensity=<num e.g. 1>, bgEffectSize=<num e.g. 1>, frosted=true|false"}
        name = parts[1].lower().replace(" ", "-")
        colors = {"bg": parts[2], "fg": parts[3], "panel": parts[4], "border": parts[5], "red": parts[6]}
        # Validate base hex colors
        import re as _re
        for k, v in colors.items():
            if not _re.match(r'^#[0-9a-fA-F]{6}$', v):
                return {"error": f"Invalid hex color for {k}: '{v}'. Use format #RRGGBB"}
        # Parse optional advanced key=value pairs
        adv_keys = {
            "userBubbleBg", "aiBubbleBg", "bubbleBorder", "sidebarBg",
            "sectionAccent", "brandColor", "inputBg", "inputBorder",
            "sendBtnBg", "sendBtnHover", "codeBg", "codeFg",
            "toggleBg", "toggleActive", "accentPrimary", "accentError",
        }
        advanced = {}
        # Background-effect fields (animated pattern + frosted glass). Different
        # value types than the hex-only advanced keys, so parse separately.
        _BG_PATTERNS = {"none", "dots", "synapse", "rain", "constellations",
                        "perlin-flow", "petals", "sparkles", "embers"}
        bg = {}
        for part in parts[7:]:
            if "=" not in part:
                continue
            ak, av = part.split("=", 1)
            if ak in adv_keys:
                if not _re.match(r'^#[0-9a-fA-F]{6}$', av):
                    return {"error": f"Invalid hex color for advanced key {ak}: '{av}'. Use format #RRGGBB"}
                advanced[ak] = av
            elif ak == "bgPattern":
                if av not in _BG_PATTERNS:
                    return {"error": f"Invalid bgPattern '{av}'. Use one of: {', '.join(sorted(_BG_PATTERNS))}"}
                bg["pattern"] = av
            elif ak == "bgEffectColor":
                if not _re.match(r'^#[0-9a-fA-F]{6}$', av):
                    return {"error": f"Invalid hex color for bgEffectColor: '{av}'. Use format #RRGGBB"}
                bg["effectColor"] = av
            elif ak in ("bgEffectIntensity", "bgEffectSize"):
                try:
                    bg["effectIntensity" if ak == "bgEffectIntensity" else "effectSize"] = float(av)
                except ValueError:
                    return {"error": f"Invalid number for {ak}: '{av}'"}
            elif ak == "frosted":
                bg["frosted"] = av.lower() in ("true", "1", "yes", "on")
        if advanced:
            colors["advanced"] = advanced
        return {
            "ui_event": "create_theme",
            "theme_name": name,
            "colors": colors,
            "bg": bg or None,
            "results": f"Custom theme '{name}' created and applied"
                       + (f" with {len(advanced)} advanced overrides" if advanced else "")
                       + (f" + background effect ({bg.get('pattern', 'frosted' if bg.get('frosted') else 'custom')})" if bg else ""),
        }

    elif action == "highlight":
        selector = parts[1] if len(parts) > 1 else ""
        label = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not selector:
            return {"error": "highlight needs: highlight <css-selector> [label]"}
        return {
            "ui_event": "highlight",
            "selector": selector,
            "label": label,
            "results": f"Highlighting '{selector}'",
        }

    elif action == "clear_highlight":
        return {
            "ui_event": "clear_highlight",
            "results": "Highlights cleared",
        }

    elif action == "open_panel":
        # Open a top-level panel/modal: documents/library, gallery,
        # email, sessions, notes, memories, skills, settings, cookbook.
        panel = parts[1].lower() if len(parts) > 1 else ""
        _panel_aliases = {
            "documents": "documents",
            "document": "documents",
            "doc": "documents",
            "docs": "documents",
            "library": "documents",
            "doclib": "documents",
            "gallery": "gallery",
            "images": "gallery",
            "email": "email",
            "emails": "email",
            "inbox": "email",
            "mail": "email",
            "sessions": "sessions",
            "chats": "sessions",
            "history": "sessions",
            "notes": "notes",
            "note": "notes",
            "todo": "notes",
            "todos": "notes",
            "memories": "memories",
            "memory": "memories",
            "brain": "memories",
            "skills": "skills",
            "journey": "journey",
            "learning": "journey",
            "settings": "settings",
            "preferences": "settings",
            "cookbook": "cookbook",
            "models": "cookbook",
            "llm": "cookbook",
            "serve": "cookbook",
            "serving": "cookbook",
        }
        target = _panel_aliases.get(panel)
        if not target:
            return {"error": f"Unknown panel '{panel}'. Valid: documents, gallery, email, sessions, notes, memories, skills, journey, settings, cookbook."}
        return {
            "ui_event": "open_panel",
            "panel": target,
            "results": f"Opening {target} panel",
        }

    elif action == "open_email_reply":
        # Two forms supported:
        #   open_email_reply <uid> [folder] [reply|reply-all|ai-reply]
        #   open_email_reply <uid> [folder] [reply|reply-all|ai-reply]
        #     <body text on subsequent lines or after the mode token>
        # The body text (if any) gets pre-filled into the reply draft so the
        # agent can compose-and-open in one tool call instead of opening an
        # empty draft and leaving the user to wonder what happened.
        first_line = lines[0].strip()
        parts = first_line.split(maxsplit=4)
        uid = parts[1].strip() if len(parts) > 1 else ""
        folder = parts[2].strip() if len(parts) > 2 else "INBOX"
        mode = parts[3].strip().lower() if len(parts) > 3 else "reply"
        # Body: everything on the first line after the mode token, plus any
        # subsequent lines. Allows multi-line bodies.
        inline_body = parts[4] if len(parts) > 4 else ""
        rest_lines = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        body = (inline_body + ("\n" + rest_lines if rest_lines else "")).strip()
        if not uid:
            return {"error": "open_email_reply needs: open_email_reply <uid> [folder] [reply|reply-all|ai-reply] [body text]"}
        if mode not in ("reply", "reply-all", "ai-reply"):
            mode = "reply"
        # Body is REQUIRED for the agent path. Opening an empty draft is what
        # users do by clicking the Reply button — they don't ask the agent
        # for that. Every agent invocation of open_email_reply MUST include
        # the body. Reject empty so the agent retries with the content the
        # user asked for. Exception: ai-reply mode triggers the existing
        # AI-Reply path on the frontend which generates its own body.
        if not body and mode != "ai-reply":
            return {
                "error": (
                    "open_email_reply called without body. The agent path REQUIRES a body — "
                    "opening an empty draft is the wrong response when the user asked you to write. "
                    "Re-call with the reply text included: "
                    f"`open_email_reply {uid} {folder or 'INBOX'} {mode} <your reply text here>`. "
                    "Compose the reply now based on the open email's content and the user's request, "
                    "then call this tool again with the body. Do NOT call create_document instead."
                ),
            }
        result = {
            "ui_event": "open_email_reply",
            "uid": uid,
            "folder": folder or "INBOX",
            "mode": mode,
            "results": f"Opening reply draft for email UID {uid}" + (" with pre-filled body" if body else ""),
        }
        if body:
            result["body"] = body
        return result

    elif action == "get_toggles":
        return {
            "results": (
                "Toggle states are managed client-side in localStorage. "
                "Available toggles: web, bash, rag, research, incognito, document_editor. "
                "Use 'toggle <name> <on|off>' to change them."
            )
        }

    else:
        return {"error": f"Unknown action '{action}'. Use: toggle, set_mode, switch_model, set_theme, highlight, clear_highlight, get_toggles"}


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _parse_image_generation_content(content: str) -> Dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                size_value = data.get("size")
                return {
                    "prompt": str(data.get("prompt") or "").strip(),
                    "model": str(data.get("model") or "").strip(),
                    "size": str(size_value or "").strip(),
                    "size_explicit": bool(str(size_value or "").strip()),
                    "quality": str(data.get("quality") or "medium").strip(),
                    "reference_image_urls": [
                        str(u) for u in (data.get("reference_image_urls") or []) if isinstance(u, str) and u
                    ],
                }
        except (TypeError, ValueError):
            pass

    lines = raw.split("\n")
    return {
        "prompt": lines[0].strip() if lines else "",
        "model": lines[1].strip() if len(lines) > 1 and lines[1].strip() else "",
        "size": lines[2].strip() if len(lines) > 2 and lines[2].strip() else "",
        "size_explicit": len(lines) > 2 and bool(lines[2].strip()),
        "quality": lines[3].strip() if len(lines) > 3 and lines[3].strip() else "medium",
        "reference_image_urls": [],
    }


def _fit_diffusion_size(width: int, height: int, max_side: int = IMAGE_EDIT_MAX_SIDE, multiple: int = 16) -> tuple[int, int]:
    try:
        width = int(width)
        height = int(height)
    except Exception:
        return max_side, max_side
    if width <= 0 or height <= 0:
        return max_side, max_side
    scale = min(max_side / max(width, height), 1.0)
    target_w = max(multiple, int(round(width * scale / multiple)) * multiple)
    target_h = max(multiple, int(round(height * scale / multiple)) * multiple)
    return target_w, target_h


def _fit_diffusion_size_from_bytes(image_bytes: bytes, max_side: int = IMAGE_EDIT_MAX_SIDE) -> tuple[int, int]:
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as im:
            return _fit_diffusion_size(im.width, im.height, max_side=max_side)
    except Exception:
        return max_side, max_side


def _image_ref_to_file(url: str, owner: Optional[str] = None):
    """Convert trusted local/data image refs to httpx multipart file tuples."""
    from pathlib import Path

    if not isinstance(url, str) or not url:
        return None

    if url.startswith("data:image/"):
        header, _, b64 = url.partition(",")
        if not b64:
            return None
        mime = header[5:].split(";", 1)[0] or "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        try:
            return ("image", (f"reference{ext}", base64.b64decode(b64), mime))
        except Exception:
            return None

    if url.startswith("/api/generated-image/"):
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
        base = Path(GENERATED_IMAGES_DIR).resolve()
        path = (base / filename).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            return None
        if not path.is_file():
            return None
        if owner:
            try:
                from core.database import SessionLocal, GalleryImage
                db = SessionLocal()
                try:
                    owned = (
                        db.query(GalleryImage.id)
                        .filter(
                            GalleryImage.filename == filename,
                            GalleryImage.owner == owner,
                            GalleryImage.is_active == True,
                        )
                        .first()
                    )
                finally:
                    db.close()
                if not owned:
                    return None
            except Exception:
                return None
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return ("image", (path.name, path.read_bytes(), mime))

    if url.startswith("upload:"):
        upload_id = url.split(":", 1)[1]
        try:
            from src.constants import BASE_DIR, UPLOAD_DIR
            from src.upload_handler import UploadHandler
            handler = UploadHandler(BASE_DIR, UPLOAD_DIR)
            info = handler.resolve_upload(upload_id, owner=owner)
        except Exception:
            info = None
        if not info or not str(info.get("mime") or "").startswith("image/"):
            return None
        path = Path(info.get("path") or "")
        if not path.is_file():
            return None
        mime = info.get("mime") or mimetypes.guess_type(path.name)[0] or "image/png"
        return ("image", (path.name, path.read_bytes(), mime))

    return None


def _adopt_image_model_for_session(
    session_id: Optional[str],
    url: str,
    model_id: str,
    headers: Optional[Dict],
    owner: Optional[str] = None,
) -> Dict:
    if not session_id or not model_id:
        return {}
    previous: Dict = {}
    if _session_manager:
        try:
            existing = _session_manager.get_session(session_id)
            found_previous = False
            for msg in reversed(getattr(existing, "history", []) or []):
                if found_previous:
                    break
                meta = getattr(msg, "metadata", None) or {}
                for ev in reversed(meta.get("tool_events") or []):
                    if isinstance(ev, dict) and ev.get("image_previous_model") and ev.get("image_previous_endpoint_url"):
                        previous = {
                            "previous_model": ev.get("image_previous_model"),
                            "previous_endpoint_url": ev.get("image_previous_endpoint_url"),
                        }
                        found_previous = True
                        break
        except Exception:
            previous = {}
    try:
        from src.database import SessionLocal as SL2, Session as DbSess2, ModelEndpoint as DbModelEndpoint
        from src.endpoint_resolver import normalize_base
        from src.auth_helpers import owner_filter

        def _matching_endpoint_id(db, endpoint_url: str) -> str:
            if not endpoint_url:
                return ""
            try:
                target = normalize_base(endpoint_url)
            except Exception:
                target = endpoint_url.rstrip("/")
            q = db.query(DbModelEndpoint).filter(DbModelEndpoint.is_enabled == True)
            if owner:
                q = owner_filter(q, DbModelEndpoint, owner)
            for ep in q.all():
                try:
                    if normalize_base(getattr(ep, "base_url", "") or "") == target:
                        return getattr(ep, "id", "") or ""
                except Exception:
                    continue
            return ""

        def _endpoint_is_image(db, endpoint_url: str) -> bool:
            if not endpoint_url:
                return False
            try:
                target = normalize_base(endpoint_url)
            except Exception:
                target = endpoint_url.rstrip("/")
            q = db.query(DbModelEndpoint).filter(DbModelEndpoint.is_enabled == True)
            if owner:
                q = owner_filter(q, DbModelEndpoint, owner)
            for ep in q.all():
                try:
                    if normalize_base(getattr(ep, "base_url", "") or "") != target:
                        continue
                    if (getattr(ep, "model_type", None) or "llm") == "image":
                        return True
                except Exception:
                    continue
            return False

        db2 = SL2()
        try:
            q = db2.query(DbSess2).filter(DbSess2.id == session_id)
            if owner:
                q = q.filter(DbSess2.owner == owner)
            db_s = q.first()
            if db_s:
                current_is_image = str(db_s.model or "").lower().startswith(("gpt-image", "dall-e", "chatgpt-image"))
                current_is_image = current_is_image or _endpoint_is_image(db2, db_s.endpoint_url)
                if not previous or not current_is_image:
                    previous = {
                        "previous_model": db_s.model,
                        "previous_endpoint_url": db_s.endpoint_url,
                    }
                if previous and not previous.get("previous_endpoint_id"):
                    previous["previous_endpoint_id"] = _matching_endpoint_id(db2, previous.get("previous_endpoint_url") or "")
                db_s.endpoint_url = url
                db_s.model = model_id
                if headers:
                    db_s.headers = headers
                db2.commit()
        finally:
            db2.close()

        if _session_manager:
            sess = _session_manager.get_session(session_id)
            if sess and (not owner or getattr(sess, "owner", None) == owner):
                sess.endpoint_url = url
                sess.model = model_id
                if headers:
                    sess.headers = headers
    except Exception as e:
        logger.warning(f"Failed to adopt image model for session {session_id}: {e}")
    return previous


def format_ideogram_prompt(user_prompt: str, size: str, owner: Optional[str] = None) -> str:
    """Format prompt for Ideogram-4 model using JSON-based deconstruction.

    If prompt is already valid Ideogram-like JSON, returns it as-is.
    """
    trimmed = user_prompt.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        try:
            data = json.loads(trimmed)
            if isinstance(data, dict) and ("high_level_description" in data or "style_description" in data):
                return trimmed
        except Exception:
            pass

    prompt_lower = user_prompt.lower()
    medium = "photograph"
    aesthetics = "clean, realistic, minimal, natural"
    photo_style = "sharp realistic product photography"
    lighting = "soft diffused studio lighting with gentle shadows"
    background = "Clean neutral studio background, no text, no people."

    if any(k in prompt_lower for k in ["painting", "watercolor", "oil painting", "canvas"]):
        medium = "painting"
        aesthetics = "artistic, colorful, detailed, expressive"
        photo_style = None
    elif any(k in prompt_lower for k in ["illustration", "vector", "drawing", "sketch", "digital art"]):
        medium = "illustration"
        aesthetics = "clean, minimal, graphic"
        photo_style = None
    elif "anime" in prompt_lower or "manga" in prompt_lower:
        medium = "illustration"
        aesthetics = "anime style, vibrant, detailed"
        photo_style = None
    elif "pixel" in prompt_lower:
        medium = "pixel art"
        aesthetics = "retro, pixelated, 8-bit"
        photo_style = None

    if "dark" in prompt_lower or "night" in prompt_lower:
        lighting = "dramatic low-key lighting with high contrast"
    elif "bright" in prompt_lower or "sun" in prompt_lower or "day" in prompt_lower:
        lighting = "bright natural sunlight"
    elif "neon" in prompt_lower or "cyberpunk" in prompt_lower:
        lighting = "vibrant neon glow with colored highlights"

    color_map = {
        "red": "#B32020",
        "blue": "#2060B3",
        "green": "#20B340",
        "yellow": "#F3D020",
        "orange": "#E37020",
        "purple": "#8020B3",
        "pink": "#E370A0",
        "brown": "#8B5A2B",
        "black": "#202020",
        "white": "#F5F0E8",
        "grey": "#808080",
        "gray": "#808080",
    }

    palette = []
    for color, hex_val in color_map.items():
        if color in prompt_lower:
            palette.append(hex_val)
    if not palette:
        palette = ["#B32020", "#8B5A2B", "#F5F0E8", "#202020"]
    else:
        while len(palette) < 4:
            palette.append("#808080")
        palette = palette[:4]

    canvas_desc = "Square 1024 by 1024 image"
    if size:
        parts = size.lower().split("x")
        if len(parts) == 2:
            w, h = parts[0], parts[1]
            try:
                w_val = int(w)
                h_val = int(h)
                orientation = "square"
                if w_val > h_val:
                    orientation = "landscape"
                elif h_val > w_val:
                    orientation = "upright"
                canvas_desc = f"{orientation.capitalize()} {w_val} by {h_val} image"
            except ValueError:
                pass

    clean_desc = user_prompt.strip()
    for phrase in [", studio photo", ", photography", "studio photo", "photography"]:
        clean_desc = clean_desc.replace(phrase, "")
    clean_desc = clean_desc.strip().strip(",").strip(".")
    if clean_desc:
        clean_desc = clean_desc[0].upper() + clean_desc[1:]

    # Load template from settings (per-user if owner is specified)
    from src.settings import get_user_setting, DEFAULT_JSON_TEMPLATE
    template = get_user_setting("image_prompt_json_template", owner or "", default=DEFAULT_JSON_TEMPLATE)
    if not template:
        template = DEFAULT_JSON_TEMPLATE

    w_val, h_val = 1024, 1024
    if size:
        parts = size.lower().split("x")
        if len(parts) == 2:
            try:
                w_val = int(parts[0])
                h_val = int(parts[1])
            except ValueError:
                pass

    is_full_scene = any(k in prompt_lower for k in [
        "landscape", "scenery", "scene", "poster", "panoramic", "view",
        "cityscape", "forest", "mountains", "beach", "skyline", "room",
        "interior", "background", "wide shot", "full shot", "streets"
    ])
    has_multiple = any(k in prompt_lower for k in [
        "and", "with", "group", "multiple", "several", "crowd",
        "two ", "three ", "four ", "five ", "six "
    ])

    if is_full_scene or has_multiple:
        bbox = [0, 0, w_val, h_val]
    else:
        bbox = [int(w_val * 0.3125), int(h_val * 0.3125), int(w_val * 0.742), int(h_val * 0.703)]

    main_subject_description = clean_desc

    try:
        # JSON-escape values
        escaped_clean_prompt = json.dumps(clean_desc)[1:-1]
        escaped_aesthetics = json.dumps(aesthetics)[1:-1]
        escaped_lighting = json.dumps(lighting)[1:-1]
        escaped_medium = json.dumps(medium)[1:-1]
        escaped_canvas = json.dumps(canvas_desc)[1:-1]
        escaped_background = json.dumps(background)[1:-1]
        escaped_photo_style = json.dumps(photo_style)[1:-1] if photo_style else ""
        escaped_main_subject = json.dumps(main_subject_description)[1:-1]

        filled = template
        filled = filled.replace("{{clean_prompt}}", escaped_clean_prompt)
        filled = filled.replace("{{aesthetics}}", escaped_aesthetics)
        filled = filled.replace("{{lighting}}", escaped_lighting)
        filled = filled.replace("{{medium}}", escaped_medium)
        filled = filled.replace("{{canvas}}", escaped_canvas)
        filled = filled.replace("{{background}}", escaped_background)
        filled = filled.replace("{{photo_style}}", escaped_photo_style)
        filled = filled.replace("{{palette}}", json.dumps(palette))
        filled = filled.replace("{{bbox}}", json.dumps(bbox))
        filled = filled.replace("{{main_subject_description}}", escaped_main_subject)

        # Test if valid JSON
        json.loads(filled)
        return filled
    except Exception as e:
        logger.warning(f"Failed to substitute custom prompt template: {e}. Falling back to default dictionary.")

    ideogram_dict = {
        "high_level_description": clean_desc,
        "style_description": {
            "aesthetics": aesthetics,
            "lighting": lighting,
            "medium": medium,
            "color_palette": palette
        },
        "compositional_deconstruction": {
            "canvas": canvas_desc,
            "background": background,
            "elements": [
                {
                    "type": "obj",
                    "bbox": bbox,
                    "desc": main_subject_description
                }
            ]
        }
    }
    if photo_style:
        ideogram_dict["style_description"]["photo"] = photo_style

    return json.dumps(ideogram_dict)


# ── Ask-before-generate: pending image requests awaiting a model choice ────
# When the `ask_image_model` setting is ON, generate_image / ai_edit_image
# tool calls are stashed here instead of executing; the frontend shows a
# model-picker card and confirms via POST /api/chat/image-choice/<session>.
# Bounded: one pending request per session, capped total.
_PENDING_IMAGE_REQUESTS: Dict[str, Dict] = {}
_PENDING_IMAGE_CAP = 64


def stash_pending_image_request(session_id: str, tool: str, content: str, owner: Optional[str]) -> None:
    if len(_PENDING_IMAGE_REQUESTS) >= _PENDING_IMAGE_CAP and session_id not in _PENDING_IMAGE_REQUESTS:
        # Drop the oldest entry (insertion order) to stay bounded.
        _PENDING_IMAGE_REQUESTS.pop(next(iter(_PENDING_IMAGE_REQUESTS)), None)
    _PENDING_IMAGE_REQUESTS[session_id] = {
        "tool": tool,
        "content": content,
        "owner": owner,
        "created": time.time(),
    }


def pop_pending_image_request(session_id: str, owner: Optional[str]) -> Optional[Dict]:
    """Pop the pending request for a session — only for the owner who queued it."""
    pending = _PENDING_IMAGE_REQUESTS.get(session_id)
    if not pending:
        return None
    if (pending.get("owner") or None) != (owner or None):
        return None
    return _PENDING_IMAGE_REQUESTS.pop(session_id, None)


# Model-id substrings that unambiguously denote an image-generation model.
# Used to AUTO-DETECT image models by name when nothing is explicitly
# configured — so a local endpoint serving FLUX/ERNIE-Image/SDXL "just works"
# without the user having to set a default or mark models by hand.
_IMAGE_MODEL_NAME_RE = re.compile(
    r"(?:flux|sdxl|sd3|sd-?[0-9]|stable[-_]?diffusion|(?<![a-z])diffusion|"
    r"dall[-_]?e|ideogram|imagen|(?<![a-z])image(?![a-z])|kandinsky|"
    r"playground[-_]?v|pixart|kolors|hunyuan[-_]?image|ernie[-_]?image|"
    r"qwen[-_]?image|cogview|omnigen|seedream|recraft|luma[-_]?photon|"
    r"gpt[-_]?image)",
    re.IGNORECASE,
)


def _image_endpoints(owner: Optional[str] = None):
    """Enabled endpoints (owner + shared), newest query — helper for resolution."""
    from src.database import SessionLocal, ModelEndpoint
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        # Detach the rows we need into plain tuples so the session can close.
        return [
            (
                ep.name,
                getattr(ep, "image_models", None),
                getattr(ep, "model_type", None),
                getattr(ep, "cached_models", None),
            )
            for ep in q.all()
        ]
    finally:
        db.close()


def _first_marked_image_model(owner: Optional[str] = None) -> str:
    """Resolve an image model for `auto` generation without an explicit
    `image_model` setting, in priority order:

      1. Per-model image marks (image_models list) — the user explicitly
         designated these in AI Defaults.
      2. An endpoint whose whole model_type is 'image'.
      3. Name auto-detection — any cached model whose id looks like an image
         model (FLUX, SDXL, ERNIE-Image, dall-e, …).

    Returns `model@endpoint` (bound to the exact endpoint, since mixed
    endpoints can share model ids) or "" if nothing matched."""
    try:
        rows = _image_endpoints(owner)
    except Exception as e:
        logger.debug("Image endpoint scan failed: %s", e)
        return ""

    def _ids(raw):
        if not raw:
            return []
        try:
            v = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (TypeError, ValueError):
            return []
        return [str(m) for m in (v or []) if m]

    # 1. Explicit per-model marks.
    for name, image_models, _mtype, _cached in rows:
        marked = _ids(image_models)
        if marked:
            return f"{marked[0]}@{name}"

    # 2. Whole endpoint marked as an image endpoint.
    for name, _image_models, mtype, cached in rows:
        if str(mtype or "").lower() == "image":
            cids = _ids(cached)
            if cids:
                return f"{cids[0]}@{name}"

    # 3. Auto-detect by model name across every enabled endpoint.
    for name, _image_models, _mtype, cached in rows:
        for mid in _ids(cached):
            if _IMAGE_MODEL_NAME_RE.search(mid):
                return f"{mid}@{name}"

    return ""


def _resolve_image_model_with_fallback(model_spec: str, owner: Optional[str] = None):
    """Resolve `model_spec` to (endpoint_url, model_id, headers), degrading a
    bogus name to the user's real image model instead of failing.

    Chat models routinely INVENT an image model name ("flux", "sdxl") they
    were never told exists. Hard-failing on that guess broke direct generation
    even when a perfectly good image model was configured/marked — so on a
    resolution miss, retry with the configured `image_model` setting, then the
    per-model marks / name auto-detection. Raises ValueError only when nothing
    resolves."""
    try:
        return _resolve_model(model_spec, owner=owner)
    except ValueError:
        pass
    from src.settings import get_user_setting
    fallbacks = []
    configured = str(get_user_setting("image_model", owner or "", default="") or "").strip()
    if configured:
        fallbacks.append(configured)
    marked = _first_marked_image_model(owner)
    if marked:
        fallbacks.append(marked)
    for fb in fallbacks:
        if fb.lower() == (model_spec or "").lower():
            continue
        try:
            resolved = _resolve_model(fb, owner=owner)
            logger.info("Image model %r not found; falling back to %r", model_spec, fb)
            return resolved
        except ValueError:
            continue
    raise ValueError(f"Model '{model_spec}' not found on any configured endpoint")


def list_image_model_options(owner: Optional[str] = None) -> list:
    """Model choices for the ask-before-generate picker.

    Options come from: the configured image/edit models, every enabled
    endpoint marked model_type='image' (each of its models as model@endpoint),
    and 'auto'. Deduped, labeled for display."""
    options = []
    seen = set()

    def add(spec: str, label: str):
        key = (spec or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        options.append({"spec": spec.strip(), "label": label.strip() or spec.strip()})

    add("auto", "Auto (configured default)")
    try:
        from src.settings import get_setting
        for skey, slabel in (("image_model", "Default gen model"), ("image_edit_model", "Default edit model")):
            spec = str(get_setting(skey, "", owner=owner) or "").strip()
            if spec:
                add(spec, f"{spec.split('@')[0].split('/')[-1]} · {slabel}")
    except Exception:
        pass
    try:
        from core.database import SessionLocal, ModelEndpoint
        db = SessionLocal()
        try:
            rows = (
                db.query(ModelEndpoint)
                .filter(ModelEndpoint.is_enabled == True)  # noqa: E712
                .all()
            )
        finally:
            db.close()
        def _id_list(raw):
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = []
            return [str(m) for m in (raw or []) if m]

        for ep in rows:
            ep_is_image = str(getattr(ep, "model_type", "") or "").lower() == "image"
            # Per-model marks — for mixed endpoints serving chat AND image
            # models (endpoint-level model_type is too coarse there).
            marked = _id_list(getattr(ep, "image_models", None))
            for mid in marked:
                add(f"{mid}@{ep.name}", f"{mid.split('/')[-1]} · {ep.name}")
            if not ep_is_image:
                continue
            # Whole endpoint is image-typed: offer every model on it.
            models = _id_list(getattr(ep, "cached_models", None) or getattr(ep, "models", None))
            for mid in models:
                add(f"{mid}@{ep.name}", f"{mid.split('/')[-1]} · {ep.name}")
            if not models and not marked:
                # Endpoint marked image-capable but no model list — offer the
                # endpoint itself, resolved by name downstream.
                add(f"auto@{ep.name}", f"Auto · {ep.name}")
    except Exception as e:
        logger.debug("list_image_model_options endpoint scan failed: %s", e)
    return options


def apply_image_model_choice(content: str, model_spec: str) -> str:
    """Rewrite a stashed generate/edit content payload with the chosen model."""
    parsed = _parse_image_generation_content(content)
    parsed["model"] = (model_spec or "auto").strip()
    out = {
        "prompt": parsed.get("prompt", ""),
        "model": parsed["model"],
        "quality": parsed.get("quality") or "medium",
    }
    if parsed.get("size_explicit") and parsed.get("size"):
        out["size"] = parsed["size"]
    if parsed.get("reference_image_urls"):
        out["reference_image_urls"] = parsed["reference_image_urls"]
    return json.dumps(out)


async def do_generate_image(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    reference_image_urls: Optional[list] = None,
    adopt_model: bool = True,
    denoising_strength: Optional[float] = None,
) -> Dict:
    """Generate an image using an image-capable model (e.g. gpt-image-1).

    Content format:
      Line 1: prompt describing the image
      Line 2: model name (optional; omit or use "auto" to use the configured image model / auto-detect)
      Line 3: size (optional, defaults to 1024x1024)
      Line 4: quality (optional, defaults to medium — options: low, medium, high, auto)
    """
    import httpx
    import os
    from pathlib import Path
    from src.url_safety import check_outbound_url

    parsed = _parse_image_generation_content(content)
    prompt = parsed["prompt"]
    model_spec = parsed["model"]
    if model_spec.lower() == "auto":
        model_spec = ""
    size = parsed["size"] or "1024x1024"
    size_explicit = bool(parsed.get("size_explicit"))
    quality = parsed["quality"] or "medium"
    refs = list(parsed.get("reference_image_urls") or [])
    for ref in reference_image_urls or []:
        if isinstance(ref, str) and ref and ref not in refs:
            refs.append(ref)
    refs = refs[:16]

    if not prompt:
        return {"error": "Image prompt is required (line 1)"}

    # Load admin settings for defaults
    try:
        from src.settings import get_user_setting
    except Exception:
        def get_user_setting(key, owner, default=""): return default

    # Use admin-configured model/quality if not specified by the tool call
    if not model_spec:
        if reference_image_urls:
            model_spec = get_user_setting("image_edit_model", owner or "", default="")
        if not model_spec:
            model_spec = get_user_setting("image_model", owner or "", default="")
    try:
        from src.settings import load_settings
        _settings = load_settings()
    except Exception:
        _settings = {}
    if quality == "medium" and _settings.get("image_quality"):
        quality = _settings["image_quality"]

    # Auto-detect best available image model if still not set.
    # FIRST: per-model image marks. A mixed endpoint (model_type='llm') that
    # also serves image models is marked per-model via AI Defaults
    # (image_models JSON list). The default `image_model` setting can be empty
    # while these marks exist — without checking them here, "auto" resolution
    # skipped straight to probing cloud gpt-image models and failed with "No
    # image model found", even though the user HAD marked image models.
    if not model_spec:
        _mark = await asyncio.to_thread(_first_marked_image_model, owner)
        if _mark:
            model_spec = _mark
            logger.info("Image auto-resolve: using per-model mark %r", model_spec)

    if not model_spec:
        for candidate in ("gpt-image-1.5", "gpt-image-1", "dall-e-3"):
            try:
                await asyncio.to_thread(_resolve_model, candidate, owner=owner)
                model_spec = candidate
                break
            except ValueError:
                continue
        # Fallback: find any locally registered image-type endpoint
        if not model_spec:
            try:
                from src.database import SessionLocal, ModelEndpoint
                from src.auth_helpers import owner_filter
                import httpx as _req
                _idb = SessionLocal()
                try:
                    _img_q = _idb.query(ModelEndpoint).filter(
                        ModelEndpoint.is_enabled == True,
                        ModelEndpoint.model_type == "image",
                    )
                    if owner:
                        _img_q = owner_filter(_img_q, ModelEndpoint, owner)
                    _img_eps = _img_q.all()
                    for _iep in _img_eps:
                        _ibase = _iep.base_url.rstrip("/")
                        if not _ibase.endswith("/v1"):
                            _ibase += "/v1"
                        try:
                            _r = _req.get(_ibase + "/models", timeout=3)
                            _r.raise_for_status()
                            _data = _r.json()
                            _ditems = _data if isinstance(_data, list) else (_data.get("data") or [])
                            _mids = [m.get("id") for m in _ditems if isinstance(m, dict) and m.get("id")]
                            if _mids:
                                model_spec = _mids[0]
                                break
                        except Exception:
                            continue
                finally:
                    _idb.close()
            except Exception:
                pass
        if not model_spec:
            return {"error": "No image model found. Configure one in Admin → Image Generation."}

    # Resolve the model to find the right endpoint
    try:
        url, model_id, headers = await asyncio.to_thread(
            _resolve_image_model_with_fallback, model_spec, owner
        )
    except ValueError:
        return {"error": f"No endpoint found with image model '{model_spec}'. "
                "Configure an OpenAI-compatible endpoint with image generation support."}

    # Detect if this is a GPT image model vs DALL-E vs local diffusion
    is_openai_api = "api.openai.com" in url
    is_gpt_image = "gpt-image" in model_id.lower()
    is_dalle = "dall-e" in model_id.lower()

    # Build the images endpoint URL from the chat completions URL
    base_url = url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
    images_url = base_url + "/images/generations"
    edits_url = base_url + "/images/edits"

    # Validate size for cloud image models (local diffusion accepts any WxH)
    valid_gpt_sizes = {"1024x1024", "1024x1536", "1536x1024", "auto"}
    valid_dalle3_sizes = {"1024x1024", "1024x1792", "1792x1024"}
    if is_openai_api and is_gpt_image and size not in valid_gpt_sizes:
        size = "1024x1024"
    elif is_openai_api and is_dalle and size not in valid_dalle3_sizes:
        size = "1024x1024"

    # Format prompt if the model uses JSON prompt format (e.g. Ideogram-4)
    from src.settings import get_user_setting, get_setting
    prompt_format_setting = get_user_setting("image_prompt_format", owner or "", default="auto")

    if prompt_format_setting == "json":
        format_type = "json"
    elif prompt_format_setting == "string":
        format_type = "string"
    else:  # auto
        format_type = "json" if "ideogram" in model_id.lower() else "string"

    if format_type == "json":
        prompt = format_ideogram_prompt(prompt, size, owner=owner)
        logger.info(f"Ideogram formatted prompt: {prompt}")

    payload = {
        "model": model_id,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }

    local_width, local_height = 1024, 1024
    try:
        _sw, _sh = (size or "1024x1024").lower().split("x", 1)
        local_width, local_height = int(_sw), int(_sh)
    except Exception:
        pass

    # GPT image models support OpenAI's quality field. Some local/proxy image
    # backends (including llama-swap -> stable-diffusion.cpp) reject or mishandle
    # unknown OpenAI fields, so keep local payloads minimal.
    if is_openai_api and is_gpt_image:
        if quality in ("low", "medium", "high", "auto"):
            payload["quality"] = quality
        else:
            payload["quality"] = "medium"
            quality = "medium"

    ref_files = [_image_ref_to_file(ref, owner=owner) for ref in refs]
    ref_files = [item for item in ref_files if item]
    if refs and not ref_files:
        return {"error": "Failed to load reference image(s). The image files may have been deleted or are inaccessible."}
    use_edits_endpoint = bool(ref_files)
    if use_edits_endpoint and not is_openai_api and not size_explicit:
        _, (_, content_bytes, _) = ref_files[0]
        local_width, local_height = _fit_diffusion_size_from_bytes(content_bytes)
        size = f"{local_width}x{local_height}"
        payload["size"] = size
        if format_type == "json":
            prompt = format_ideogram_prompt(parsed["prompt"], size, owner=owner)
            payload["prompt"] = prompt

    logger.info(
        f"Image generation: model={model_id}, size={size}, quality={quality}, "
        f"refs={len(ref_files)}, prompt={prompt[:80]}"
    )

    def _local_image_backend_hint(status_code: int, error_text: str) -> str:
        if "api.openai.com" in url or status_code < 500:
            return ""
        lowered = (error_text or "").lower()
        if not any(k in lowered for k in ("no result", "no image", "empty response", "oom", "out of memory", "cuda")):
            return ""
        return (
            " The local image backend did not return a final image. If its logs show sampling completed, "
            "check the decode step too; VAE/CUDA out-of-memory is a common cause. Try a smaller size, "
            "free VRAM, or move/tiling the VAE decode."
        )

    try:
        # GPT/OpenAI image models can take 30-120s+ depending on quality
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=900.0, write=30.0, pool=30.0)) as client:
            if use_edits_endpoint:
                if is_openai_api and is_gpt_image:
                    edit_data = {k: str(v) for k, v in payload.items() if k != "n"}
                    edit_data["input_fidelity"] = "high"
                    resp = await client.post(edits_url, data=edit_data, files=ref_files, headers=headers)
                elif is_openai_api:
                    edit_data = {k: str(v) for k, v in payload.items() if k != "n"}
                    resp = await client.post(edits_url, data=edit_data, files=ref_files, headers=headers)
                else:
                    # Self-hosted/local diffusion img2img
                    _, (_, content_bytes, _) = ref_files[0]
                    ref_b64 = base64.b64encode(content_bytes).decode()

                    strength = denoising_strength if denoising_strength is not None else 0.75

                    img2img_payload = {
                        "image": ref_b64,
                        "prompt": prompt,
                        "model": model_id,
                        "size": f"{local_width}x{local_height}",
                        "strength": strength,
                    }
                    base_root = url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
                    base_root_no_v1 = base_root[:-3] if base_root.endswith("/v1") else base_root

                    candidates = [
                        (base_root + "/images/img2img", "json", img2img_payload),
                        (base_root + "/images/variations", "json", img2img_payload),
                        (base_root_no_v1 + "/sdapi/v1/img2img", "json_a1111", {
                            "model": model_id,
                            "init_images": [f"data:image/png;base64,{ref_b64}"],
                            "prompt": prompt,
                            "width": local_width,
                            "height": local_height,
                            "denoising_strength": strength,
                            "override_settings": {"sd_model_checkpoint": model_id} if model_id else {},
                        })
                    ]

                    resp = None
                    last_err_text = ""
                    local_img2img_timeout = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)
                    for target_url, kind, pl in candidates:
                        try:
                            resp = await client.post(target_url, json=pl, headers=headers, timeout=local_img2img_timeout)
                            if resp.status_code == 200:
                                break
                            else:
                                last_err_text = f"{target_url} returned {resp.status_code}: {resp.text[:200]}"
                        except Exception as e:
                            last_err_text = f"{target_url} failed: {e}"
                            continue

                    if not resp or resp.status_code != 200:
                        endpoint = edits_url
                        error_text = last_err_text or (resp.text[:500] if resp else "No endpoint responded")
                        status_code = resp.status_code if resp else 0
                        hint = _local_image_backend_hint(status_code, error_text)
                        return {"error": f"Image generation failed ({status_code or 'error'}) for {model_id} at local img2img: {error_text}{hint}"}
            else:
                resp = await client.post(images_url, json=payload, headers=headers)

            if not use_edits_endpoint or is_openai_api:
                if resp.status_code != 200:
                    error_text = resp.text[:500]
                    try:
                        err_json = resp.json()
                        error_text = err_json.get("error", {}).get("message", error_text) if isinstance(err_json.get("error"), dict) else str(err_json.get("error", error_text))
                    except Exception:
                        pass
                    if not error_text:
                        error_text = "empty response body"
                    endpoint = edits_url if use_edits_endpoint else images_url
                    hint = _local_image_backend_hint(resp.status_code, error_text)
                    return {"error": f"Image generation failed ({resp.status_code}) for {model_id} at {endpoint}: {error_text}{hint}"}

            res_json = resp.json()
            if use_edits_endpoint and not is_openai_api:
                # Normalize local diffusion img2img output to OpenAI format: {"data": [{"b64_json": ...}]}
                img_b64 = None
                if isinstance(res_json, dict):
                    if isinstance(res_json.get("image"), str) and res_json.get("image"):
                        img_b64 = res_json["image"]
                    elif res_json.get("images") and isinstance(res_json["images"], list):
                        img_b64 = res_json["images"][0]
                        if isinstance(img_b64, str) and img_b64.startswith("data:"):
                            img_b64 = img_b64.split(",", 1)[1]
                    elif res_json.get("data") and isinstance(res_json["data"], list):
                        item = res_json["data"][0]
                        if isinstance(item, dict):
                            img_b64 = item.get("b64_json") or item.get("url")

                if isinstance(img_b64, str) and img_b64:
                    if img_b64.startswith("http"):
                        data = {"data": [{"url": img_b64}]}
                    else:
                        data = {"data": [{"b64_json": img_b64}]}
                else:
                    return {"error": f"Local diffusion endpoint returned no valid image in response: {res_json}"}
            else:
                data = res_json

            if not isinstance(data, dict):
                return {"error": f"Image API returned unexpected response type: {type(data).__name__}"}
            images = data.get("data", [])
            if not images:
                return {"error": "No images returned from API"}

            img = images[0]
            image_url = None
            image_id = None

            def _save_to_gallery(filename: str) -> Tuple[str, str]:
                """Persist gallery metadata once; return (id, canonical filename)."""
                _gdb = None
                try:
                    from src.database import SessionLocal as _GallerySL, GalleryImage

                    img_path = Path(GENERATED_IMAGES_DIR) / filename
                    content = img_path.read_bytes()
                    file_hash = hashlib.sha256(content).hexdigest()
                    width = height = None
                    try:
                        from io import BytesIO
                        from PIL import Image
                        with Image.open(BytesIO(content)) as im:
                            width, height = im.width, im.height
                    except Exception:
                        pass

                    _gdb = _GallerySL()
                    existing = _gdb.query(GalleryImage).filter(
                        GalleryImage.filename == filename,
                        GalleryImage.is_active == True,  # noqa: E712
                    ).first()
                    if existing:
                        return existing.id, existing.filename

                    dup_q = _gdb.query(GalleryImage).filter(
                        GalleryImage.file_hash == file_hash,
                        GalleryImage.is_active == True,  # noqa: E712
                    )
                    if owner:
                        dup_q = dup_q.filter(GalleryImage.owner == owner)
                    else:
                        dup_q = dup_q.filter(GalleryImage.owner == None)  # noqa: E711
                    duplicate = dup_q.first()
                    if duplicate:
                        try:
                            if duplicate.filename != filename:
                                img_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return duplicate.id, duplicate.filename

                    new_id = str(uuid.uuid4())
                    _gdb.add(GalleryImage(
                        id=new_id,
                        filename=filename,
                        prompt=prompt,
                        model=model_id,
                        size=size,
                        quality=quality,
                        session_id=session_id,
                        owner=owner,
                        file_hash=file_hash,
                        file_size=len(content),
                        width=width,
                        height=height,
                    ))
                    _gdb.commit()
                    return new_id, filename
                except Exception as _ge:
                    if _gdb is not None:
                        try:
                            _gdb.rollback()
                        except Exception:
                            pass
                    logger.warning(f"Failed to save gallery record: {_ge}")
                    return "", filename
                finally:
                    if _gdb is not None:
                        _gdb.close()

            # GPT image models always return b64_json; DALL-E may return url
            if img.get("b64_json"):
                img_dir = Path(GENERATED_IMAGES_DIR)
                img_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{uuid.uuid4().hex[:12]}.png"
                img_path = img_dir / filename
                img_path.write_bytes(base64.b64decode(img.get("b64_json")))
                image_id, filename = _save_to_gallery(filename)
                image_url = f"/api/generated-image/{filename}"

            elif img.get("url"):
                # Download external URL and save locally (DALL-E returns temp URLs)
                result_url = img["url"]
                ok, reason = check_outbound_url(
                    result_url,
                    block_private=os.getenv("IMAGE_BLOCK_PRIVATE_IPS", "false").lower() == "true",
                )
                if not ok:
                    return {"error": f"Image API returned unsafe image URL: {reason}"}
                try:
                    dl_resp = httpx.get(result_url, timeout=60)
                    if dl_resp.status_code == 200:
                        img_dir = Path(GENERATED_IMAGES_DIR)
                        img_dir.mkdir(parents=True, exist_ok=True)
                        filename = f"{uuid.uuid4().hex[:12]}.png"
                        img_path = img_dir / filename
                        img_path.write_bytes(dl_resp.content)
                        image_id, filename = _save_to_gallery(filename)
                        image_url = f"/api/generated-image/{filename}"
                    else:
                        image_url = result_url  # fallback to external URL
                except Exception as _dl_e:
                    logger.warning(f"Failed to download DALL-E image: {_dl_e}")
                    image_url = result_url  # fallback to external URL
            else:
                return {"error": "Image API returned unexpected format (no b64_json or url)"}

            ref_note = f" using {len(ref_files)} reference image(s)" if ref_files else ""
            result = {
                "results": f"Generated image{ref_note} for: {prompt[:100]}",
                "image_url": image_url,
                "image_id": image_id,
                "image_prompt": prompt,
                "image_model": model_id,
                "image_size": size,
                "image_quality": quality,
            }
            return result

    except httpx.TimeoutException:
        return {"error": "Image generation timed out (900s). The model may be overloaded — try again or use quality=low."}
    except Exception as e:
        return {"error": f"Image generation error: {str(e)}"}


# ---------------------------------------------------------------------------
# Dispatcher (called from agent_tools.execute_tool_block)
# ---------------------------------------------------------------------------

async def dispatch_ai_tool(
    tool: str, content: str, session_id: Optional[str] = None, owner: Optional[str] = None
) -> Tuple[str, Dict]:
    """Dispatch an AI interaction tool. Returns (description, result_dict)."""

    if tool == "pipeline":
        desc = "pipeline: running steps"
        result = await do_pipeline(content, session_id, owner=owner)

    elif tool == "manage_memory":
        action = content.split("\n")[0].strip()[:40]
        desc = f"manage_memory: {action}"
        result = await do_manage_memory(content, session_id, owner=owner)

    elif tool == "ui_control":
        action = content.split("\n")[0].strip()[:60]
        desc = f"ui_control: {action}"
        result = await do_ui_control(content, session_id, owner=owner)

    else:
        desc = f"unknown ai tool: {tool}"
        result = {"error": f"Unknown AI interaction tool: {tool}"}

    return desc, result
