"""Assemble the "learning made visible" graph (ported from Hermes).

This graph is intentionally scoped to what the agent actually learns over
time:

- learned skills (agent-extracted / teacher-written, or any skill that has
  actually been used),
- memory entries as first-class nodes.

Skill links come from shared tags (Aegis skills have no declared
``related_skills``, so tag overlap stands in for the declared edges Hermes
uses). Memory-to-skill links are derived from lexical overlap so the graph
can answer "which learned skills are connected to the things I remember?".

Data sources are Aegis' structured stores (``SkillsManager`` /
``MemoryManager``) instead of Hermes' SKILL.md scan + MEMORY.md chunks; the
payload shape (nodes/edges/clusters/memory/stats) is kept identical so the
ported ``learning_graph_render`` module consumes it unchanged. Memory node
ids are ``memory:<uuid>`` — Aegis entries have stable ids, so the graph
never suffers the stale positional-index problem Hermes documents.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional


def _to_int_ts(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
    except Exception:
        return None


def _is_learned(skill: dict) -> bool:
    """Mirror Hermes' "agent-created or used" learning signal.

    Aegis sources: learned / teacher-escalation are agent-created; anything
    else (user, imported, taught) counts once it has real usage.
    """
    if skill.get("source") in ("learned", "teacher-escalation"):
        return True
    return int(skill.get("uses", 0) or 0) > 0


def build_skill_nodes(skills_manager, owner: Optional[str] = None) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    for s in skills_manager.load(owner=owner):
        name = str(s.get("name") or "").strip()
        if not name or name in nodes or not _is_learned(s):
            continue
        nodes[name] = {
            "id": name,
            "label": name,
            "kind": "skill",
            "timestamp": _to_int_ts(s.get("last_used")) or _to_int_ts(s.get("created")),
            "category": str(s.get("category") or "general"),
            "useCount": int(s.get("uses", 0) or 0),
            "state": str(s.get("status") or "draft"),
            "createdBy": str(s.get("source") or "learned"),
            "pinned": False,
            "tags": [str(t).strip().lower() for t in (s.get("tags") or []) if str(t).strip()],
        }
    return nodes


def build_edges(nodes: dict[str, dict]) -> list[tuple[str, str]]:
    """Undirected shared-tag edges where BOTH endpoints exist (deduped)."""
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str]] = []
    items = list(nodes.values())
    for i, node in enumerate(items):
        tags = set(node.get("tags") or [])
        if not tags:
            continue
        for other in items[i + 1:]:
            if not tags & set(other.get("tags") or []):
                continue
            a, b = sorted((node["id"], other["id"]))
            key = (a, b)
            if key not in seen:
                seen.add(key)
                edges.append(key)
    return edges


def density_stats(nodes: dict[str, dict], edges: list[tuple[str, str]]) -> dict[str, Any]:
    linked: set[str] = set()
    for a, b in edges:
        linked.add(a)
        linked.add(b)
    cats: dict[str, int] = {}
    for n in nodes.values():
        cats[n["category"]] = cats.get(n["category"], 0) + 1
    n = len(nodes) or 1
    return {
        "nodes": len(nodes),
        "related_edges": len(edges),
        "edges_per_node": round(len(edges) / n, 3),
        "linked_nodes": len(linked),
        "isolated_pct": round(100 * (n - len(linked)) / n, 1),
        "categories": len(cats),
        "agent_created": sum(
            1 for x in nodes.values()
            if x.get("createdBy") in ("learned", "teacher-escalation")
        ),
        "used": sum(1 for x in nodes.values() if x["useCount"] > 0),
        "top_categories": sorted(cats.items(), key=lambda kv: -kv[1])[:8],
    }


def _memory_cards(memory_manager, owner: Optional[str] = None) -> list[dict[str, Any]]:
    """Memory entries as readable cards. Every entry is surfaced — the graph
    shows everything."""
    cards: list[dict[str, Any]] = []
    for m in memory_manager.load(owner=owner):
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        first = text.splitlines()[0].strip()
        source = "profile" if m.get("category") == "identity" else "memory"
        cards.append(
            {
                "id": f"memory:{m.get('id')}",
                "source": source,
                "timestamp": _to_int_ts(m.get("timestamp")),
                "title": (first[:80] + "…") if len(first) > 80 else first,
                "body": text[:1200],
                "pinned": bool(m.get("pinned")),
            }
        )
    return cards


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3}


def _memory_skill_edges(memory_cards: list[dict[str, Any]], skills: list[dict]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    skill_meta = [(s, _tokenize(s["id"]), s["id"].lower()) for s in skills]
    for card in memory_cards:
        mem_id = str(card["id"])
        text = f"{card.get('title', '')}\n{card.get('body', '')}".lower()
        text_tokens = _tokenize(text)
        scored: list[tuple[int, str]] = []
        for skill, tokens, skill_name_lower in skill_meta:
            score = 0
            if skill_name_lower in text:
                score += 6
            score += len(tokens & text_tokens)
            if score > 0:
                scored.append((score, skill["id"]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for _, skill_name in scored[:4]:
            edges.append((mem_id, skill_name))
    return edges


def build_learning_graph(skills_manager, memory_manager, owner: Optional[str] = None) -> dict[str, Any]:
    """Full payload for the Journey panel.

    Focus on what is learned and actionable:
    - skills that show real learning signal (agent-created or used),
    - memory entries as first-class graph nodes connected to those skills.
    """
    learned_skills = build_skill_nodes(skills_manager, owner=owner)
    skill_edges = build_edges(learned_skills)
    memory_cards = _memory_cards(memory_manager, owner=owner)
    memory_edges = _memory_skill_edges(memory_cards, list(learned_skills.values()))

    edges = skill_edges + memory_edges
    clusters: dict[str, int] = {}
    for node in learned_skills.values():
        clusters[node["category"]] = clusters.get(node["category"], 0) + 1
    if memory_cards:
        clusters["memory"] = len(memory_cards)

    graph_nodes = [
        {k: v for k, v in n.items() if k != "tags"}
        for n in learned_skills.values()
    ]
    for card in memory_cards:
        graph_nodes.append(
            {
                "id": card["id"],
                "label": card["title"],
                "kind": "memory",
                "memorySource": card["source"],
                "timestamp": card.get("timestamp"),
                "category": "memory",
                "useCount": 0,
                "state": "active",
                "createdBy": "memory",
                "pinned": bool(card.get("pinned")),
            }
        )

    return {
        "nodes": graph_nodes,
        "edges": [{"source": a, "target": b} for a, b in edges],
        "clusters": [
            {"category": c, "count": n}
            for c, n in sorted(clusters.items(), key=lambda kv: -kv[1])
        ],
        "memory": memory_cards,
        "stats": {
            **density_stats(learned_skills, skill_edges),
            "memory_nodes": len(memory_cards),
            "memory_skill_edges": len(memory_edges),
            "learned_skills": len(learned_skills),
        },
    }
