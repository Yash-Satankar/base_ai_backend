# tests/test_redis_backoff.py
"""
Production stability: a down Redis must NOT trigger a fresh multi-second
socket probe on every `get_redis_client()` call. After one failed probe the
client stays unavailable (memory fallback in DEBUG) for a backoff window,
without re-probing.
"""

import sys
import time
import types

import pytest

from app.db import session_store


@pytest.fixture(autouse=True)
def _clean_redis_state():
    session_store._reset_redis_state()
    yield
    session_store._reset_redis_state()


def _fake_redis_module(from_url):
    """A stand-in for the `redis` package: exposes redis.Redis.from_url."""
    mod = types.ModuleType("redis")
    mod.Redis = types.SimpleNamespace(from_url=staticmethod(from_url))
    return mod


@pytest.mark.real_redis
def test_failed_connection_is_negative_cached(monkeypatch):
    probes = {"n": 0}

    def from_url(*a, **kw):
        probes["n"] += 1
        raise ConnectionError("connection refused")

    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(from_url))

    # first call probes once and fails -> None (DEBUG memory fallback)
    assert session_store.get_redis_client() is None
    assert probes["n"] == 1

    # many subsequent calls in the backoff window do NOT re-probe
    for _ in range(25):
        assert session_store.get_redis_client() is None
    assert probes["n"] == 1


@pytest.mark.real_redis
def test_backoff_calls_are_instant(monkeypatch):
    def from_url(*a, **kw):
        time.sleep(0.05)                       # stand-in for a slow socket timeout
        raise ConnectionError("timed out")

    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(from_url))

    session_store.get_redis_client()           # one slow probe
    start = time.perf_counter()
    for _ in range(50):
        session_store.get_redis_client()
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"backoff calls re-probed (took {elapsed:.3f}s for 50 calls)"


@pytest.mark.real_redis
def test_reprobes_after_backoff_window(monkeypatch):
    probes = {"n": 0}

    def from_url(*a, **kw):
        probes["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(from_url))

    session_store.get_redis_client()
    assert probes["n"] == 1

    # jump past the backoff deadline
    monkeypatch.setattr(session_store.time, "monotonic",
                        lambda: session_store._redis_down_until + 1.0)
    session_store.get_redis_client()
    assert probes["n"] == 2


@pytest.mark.real_redis
def test_mark_redis_down_trips_backoff_from_an_operation_failure(monkeypatch):
    probes = {"n": 0}

    class _Client:
        def ping(self):
            return True

    def from_url(*a, **kw):
        probes["n"] += 1
        return _Client()

    monkeypatch.setitem(sys.modules, "redis", _fake_redis_module(from_url))

    client = session_store.get_redis_client()
    assert client is not None and probes["n"] == 1

    # an operation blows up mid-session -> mark_redis_down drops client + backs off
    session_store.mark_redis_down(RuntimeError("connection reset"))
    assert session_store.get_redis_client() is None
    for _ in range(10):
        assert session_store.get_redis_client() is None
    assert probes["n"] == 1                    # never re-probed during the window


class _DeadClient:
    """Connects fine, then raises on every data operation."""
    def ping(self):
        return True

    def __getattr__(self, name):
        def _boom(*a, **kw):
            raise ConnectionError(f"redis {name} failed")
        return _boom


@pytest.mark.real_redis
@pytest.mark.parametrize("trigger", ["session_store", "job_store", "llm_client", "input_gate"])
def test_operation_failure_in_any_module_trips_the_shared_backoff(trigger):
    """A Redis *operation* failure detected by ANY of these modules opens the
    single shared backoff window (not just session_store's own probe path)."""
    # pretend we're already connected; the next data op will blow up
    session_store._reset_redis_state()
    session_store._redis_client = _DeadClient()

    if trigger == "session_store":
        from app.engine.conversation_engine import ConversationState
        session_store.save_session(ConversationState(session_id="s1"))  # setex -> boom
    elif trigger == "job_store":
        from app.services import job_store
        job_store._JobStore()._save_to_redis("j1", {"x": 1})
    elif trigger == "llm_client":
        from app.conversation import llm_client
        llm_client._add_conversation_cost("sess-x", 0.001)
    else:
        from app.guardrails import input_gate
        input_gate.record_quarantine("sess-x")

    # window is open and the client is dropped — every caller now sees Redis down
    assert session_store._redis_down_until > 0, f"{trigger} did not trip the backoff window"
    assert session_store._redis_client is None
    assert session_store.get_redis_client() is None
