# tests/test_conversation_memory.py
"""
Phase 3 — durable, project-scoped conversation memory.

- persist_checkpoint upserts one row per project (transient facts stripped)
- rehydrate warm-starts a fresh session from that row
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ConversationMemory
from app.db.repositories.memory_repo import ConversationMemoryRepository
from app.services import conversation_memory as cm
from app.engine.conversation_engine import ConversationState, ConversationStage, ProjectBlueprint


@pytest.fixture
def db():
    """A fresh in-memory async SQLite session per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    session = Session()
    try:
        yield session
    finally:
        asyncio.run(session.close())
        asyncio.run(engine.dispose())


def _blueprint(name="Shoe Store", tables=6, gst=False):
    return ProjectBlueprint(
        project_name=name, description="an online shoe store", domain="e_commerce",
        all_domains=["e_commerce"],
        modules=[{"name": "Core", "description": "core",
                  "tables": [{"name": f"t{i}", "purpose": "p"} for i in range(tables)]}],
        rules_to_apply=[1, 2], scale="medium", gst_required=gst, confirmed=True,
    )


def _state(project_id="proj-1"):
    s = ConversationState(session_id="s1")
    s.project_id = project_id
    s.requirement_summary = "an online shoe store with orders and returns"
    s.rolling_summary = "shoe store: customers order shoes, returns within 30 days"
    s.key_decisions = ["returns window is 30 days", "card payments only"]
    s.rejected_options = ["subscription boxes"]
    s.understood_aspects = {"scale": "small"}
    s.facts = {"_domain": "e_commerce", "_domain_for": "abc123", "_all_domains": ["e_commerce"],
               "_summarized_upto": 8, "_lang_ack_done": True}
    return s


# ── persist ──────────────────────────────────────────────────

def test_persist_checkpoint_upserts_one_row_per_project(db):
    st = _state()
    st.blueprint = _blueprint()

    ok = asyncio.run(cm.persist_checkpoint(st, db, reason="blueprint_confirmed"))
    assert ok is True

    async def _read():
        return await ConversationMemoryRepository(db).get_by_project("proj-1")

    row = asyncio.run(_read())
    assert row is not None
    assert row.rolling_summary.startswith("shoe store")
    assert "returns window is 30 days" in row.key_decisions
    assert row.rejected_options == ["subscription boxes"]
    assert row.last_checkpoint == "blueprint_confirmed"
    assert row.last_blueprint["project_name"] == "Shoe Store"
    # transient per-turn caches stripped, durable facts kept
    assert row.facts.get("_domain") == "e_commerce"
    for k in ("_domain_for", "_all_domains", "_summarized_upto", "_lang_ack_done"):
        assert k not in row.facts

    # second checkpoint updates the SAME row
    st.key_decisions.append("free shipping over $50")
    asyncio.run(cm.persist_checkpoint(st, db, reason="schema_complete"))

    async def _count():
        from sqlalchemy import select, func
        r = await db.execute(select(func.count()).select_from(ConversationMemory))
        return r.scalar_one()

    assert asyncio.run(_count()) == 1
    row2 = asyncio.run(_read())
    assert "free shipping over $50" in row2.key_decisions
    assert row2.last_checkpoint == "schema_complete"


def test_persist_checkpoint_skips_anonymous_session(db):
    st = _state(project_id=None)
    ok = asyncio.run(cm.persist_checkpoint(st, db, reason="session_end"))
    assert ok is False

    async def _count():
        from sqlalchemy import select, func
        r = await db.execute(select(func.count()).select_from(ConversationMemory))
        return r.scalar_one()

    assert asyncio.run(_count()) == 0


def test_durable_facts_strips_transient_keys():
    facts = {"_domain": "hr", "_domain_for": "x", "_all_domains": ["hr"],
             "_summarized_upto": 4, "_lang_ack_done": True, "_resumed": True,
             "custom_note": "keep me"}
    out = cm._durable_facts(facts)
    assert out == {"_domain": "hr", "custom_note": "keep me"}


# ── rehydrate ────────────────────────────────────────────────

def test_rehydrate_warm_starts_with_blueprint(db):
    seed = _state()
    seed.blueprint = _blueprint(name="Cobbler", tables=9)
    asyncio.run(cm.persist_checkpoint(seed, db, reason="schema_complete"))

    fresh = ConversationState(session_id="s2")   # brand new session, INITIAL, empty
    assert fresh.stage == ConversationStage.INITIAL

    found = asyncio.run(cm.rehydrate(fresh, db, "proj-1"))
    assert found is True
    assert fresh.rolling_summary.startswith("shoe store")
    assert "card payments only" in fresh.key_decisions
    assert fresh.rejected_options == ["subscription boxes"]
    assert fresh.facts["_domain"] == "e_commerce"
    assert fresh.facts["_resumed"] is True
    assert fresh.blueprint is not None and fresh.blueprint.project_name == "Cobbler"
    assert fresh.stage == ConversationStage.BLUEPRINT
    assert "_resume_summary" in fresh.facts
    # transient plumbing keys were NOT restored
    assert "_domain_for" not in fresh.facts


def test_rehydrate_without_blueprint_resumes_at_clarifying(db):
    seed = _state()
    seed.blueprint = None
    asyncio.run(cm.persist_checkpoint(seed, db, reason="session_end"))

    fresh = ConversationState(session_id="s3")
    found = asyncio.run(cm.rehydrate(fresh, db, "proj-1"))
    assert found is True
    assert fresh.blueprint is None
    assert fresh.stage == ConversationStage.CLARIFYING
    assert fresh.requirement_summary.startswith("an online shoe store")


def test_rehydrate_no_memory_is_noop(db):
    fresh = ConversationState(session_id="s4")
    found = asyncio.run(cm.rehydrate(fresh, db, "never-seen"))
    assert found is False
    assert fresh.stage == ConversationStage.INITIAL
    assert fresh.rolling_summary == ""
    assert fresh.key_decisions == []
    assert "_resumed" not in fresh.facts
