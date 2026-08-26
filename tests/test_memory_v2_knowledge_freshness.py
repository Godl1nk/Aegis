import json
from contextlib import contextmanager
from types import SimpleNamespace


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.start = 0
        self.count = None

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def offset(self, value):
        self.start = value
        return self

    def limit(self, value):
        self.count = value
        return self

    def all(self):
        end = None if self.count is None else self.start + self.count
        return self.rows[self.start:end]


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, _model):
        return _Query(self.rows)


def _row(idx, expires_at):
    return SimpleNamespace(
        id=f"knowledge-{idx}",
        owner="alice",
        text=f"Claim {idx}",
        timestamp=1000 - idx,
        source="web_validated",
        category="knowledge",
        uses=0,
        kind="knowledge",
        status="active",
        session_id=None,
        pinned=False,
        priority=0,
        confidence="medium",
        source_refs="[]",
        metadata_json=json.dumps({"expires_at": expires_at}),
    )


def test_expired_rows_before_sql_limit_do_not_hide_fresh_knowledge(tmp_path, monkeypatch):
    from src.memory_v2 import MemoryV2Store

    store = MemoryV2Store(str(tmp_path))
    rows = [_row(idx, 1) for idx in range(100)] + [_row(101, 4_000_000_000)]

    @contextmanager
    def fake_db():
        yield _DB(rows)

    monkeypatch.setattr(store, "_db", fake_db)
    monkeypatch.setattr(store, "migrate_from_json_once", lambda: None)

    result = store.load_knowledge("alice", limit=1)
    assert [item["id"] for item in result] == ["knowledge-101"]
