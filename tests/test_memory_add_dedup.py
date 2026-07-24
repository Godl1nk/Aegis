"""Agent `manage_memory add` must dedup before creating (regression: the
write path used to append near-duplicates unconditionally — see the Brain
panel showing "User's name is X" twice)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_interaction import _find_duplicate_memory, _norm_memory_text


class _FakeMgr:
    pass


def _mem(id_, text, cat="fact", owner=None):
    return {"id": id_, "text": text, "category": cat, "owner": owner}


NO_VECTOR = None  # ChromaDB down — the common real-world case


class TestExactAndSuperset:
    def test_exact_match(self):
        existing = [_mem("1", "User's name is Fong Chuan Zhe")]
        dup = _find_duplicate_memory("user's name is fong chuan zhe.", existing, _FakeMgr(), NO_VECTOR, None)
        assert dup and dup["id"] == "1"

    def test_superset_prefix_caught_without_vector(self):
        # The exact screenshot case: shorter fact already stored, agent tries
        # to add the richer superset. Prefix containment catches it.
        existing = [_mem("1", "User's name is Fong Chuan Zhe")]
        dup = _find_duplicate_memory(
            "User's name is Fong Chuan Zhe currently studying in universiti",
            existing, _FakeMgr(), NO_VECTOR, None,
        )
        assert dup and dup["id"] == "1"

    def test_reverse_superset_also_caught(self):
        # Richer stored first, then the shorter one is attempted.
        existing = [_mem("1", "User's name is Fong Chuan Zhe currently studying in universiti")]
        dup = _find_duplicate_memory(
            "User's name is Fong Chuan Zhe", existing, _FakeMgr(), NO_VECTOR, None
        )
        assert dup and dup["id"] == "1"


class TestFalsePositiveGuards:
    def test_opposite_preferences_not_merged(self):
        # 0.75 Jaccard floor keeps opposite facts distinct.
        existing = [_mem("1", "User prefers dark mode")]
        dup = _find_duplicate_memory("User prefers light mode", existing, _FakeMgr(), NO_VECTOR, None)
        assert dup is None

    def test_distinct_facts_not_merged(self):
        existing = [_mem("1", "User is studying Mechanical Engineering at UTP")]
        dup = _find_duplicate_memory(
            "AI Server has an NVIDIA RTX 2080 Ti with 22GB VRAM",
            existing, _FakeMgr(), NO_VECTOR, None,
        )
        assert dup is None

    def test_short_shared_prefix_not_over_matched(self):
        # "User likes" is a 2-token prefix — below the 3-token floor, so two
        # genuinely different short facts are not collapsed.
        existing = [_mem("1", "User likes tea")]
        dup = _find_duplicate_memory("User likes coffee", existing, _FakeMgr(), NO_VECTOR, None)
        assert dup is None

    def test_empty_returns_none(self):
        assert _find_duplicate_memory("   ", [_mem("1", "x")], _FakeMgr(), NO_VECTOR, None) is None


class TestVectorPath:
    def test_vector_match_used_when_healthy(self):
        class _Vec:
            healthy = True
            def find_similar(self, text, threshold=0.85, owner=None):
                return "1"  # semantic hit
        existing = [_mem("1", "User's name is Fong Chuan Zhe, studying at university")]
        dup = _find_duplicate_memory(
            "The user goes by Fong Chuan Zhe and is a student",
            existing, _FakeMgr(), _Vec(), None,
        )
        assert dup and dup["id"] == "1"

    def test_vector_miss_falls_through_to_text(self):
        class _Vec:
            healthy = True
            def find_similar(self, text, threshold=0.85, owner=None):
                return None
        existing = [_mem("1", "User's name is Fong Chuan Zhe")]
        # No vector hit, but exact-normalized text still matches.
        dup = _find_duplicate_memory("User's name is Fong Chuan Zhe", existing, _FakeMgr(), _Vec(), None)
        assert dup and dup["id"] == "1"


def test_norm_helper():
    assert _norm_memory_text("  User's   Name is X. ") == "user's name is x"
