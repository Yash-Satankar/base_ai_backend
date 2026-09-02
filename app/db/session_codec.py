# app/db/session_codec.py
"""
Versioned, schema-safe (de)serialisation for ConversationState.

Replaces raw pickle in the Redis session store:
  - ``encode()`` writes a JSON envelope with a schema version, safe across deploys
  - ``decode()`` reads that envelope; transparently falls back to legacy pickle
    bytes so sessions written before this change keep working until they are
    next saved (at which point they are rewritten as JSON).

Adding, removing, or renaming a ConversationState field no longer bricks live
sessions: unknown keys are dropped on load, missing keys fall back to the
dataclass default, and structural changes get a migration entry in
``_MIGRATIONS``.
"""

import json
import pickle
import logging
import dataclasses
from enum import Enum
from typing import Any, Optional

from app.engine.conversation_engine import (
    ConversationState,
    ConversationMessage,
    ProjectBlueprint,
    ConversationStage,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
_ENVELOPE_MARKER = "conversation_state_json"

# version N -> N+1 transform applied to the inner ``data`` dict.
# v1 -> v2 (Phase 2): rolling_summary / key_decisions / facts
# v2 -> v3 (Phase 3): rejected_options
# All additions carry dataclass defaults, so no explicit migration is needed
# (``_build_state`` fills missing fields from the defaults).
_MIGRATIONS: dict[int, Any] = {}


def _json_default(o: Any):
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return list(o)
    # Last resort: keep the encode alive rather than losing the whole session.
    logger.warning(
        f"session_codec: coercing unexpected {type(o).__name__} to str during encode"
    )
    return str(o)


def encode(state: ConversationState) -> bytes:
    """Serialise a ConversationState to a versioned JSON envelope (bytes)."""
    payload = {
        "__codec__": _ENVELOPE_MARKER,
        "__version__": SCHEMA_VERSION,
        "data": dataclasses.asdict(state),
    }
    return json.dumps(payload, default=_json_default).encode("utf-8")


def decode(raw: Optional[bytes]):
    """Deserialise session bytes back into a ConversationState.

    Order of attempts:
      1. JSON envelope written by ``encode()``
      2. legacy pickle bytes (pre-migration sessions)
    Returns ``None`` if the payload cannot be understood at all.
    """
    if not raw:
        return None

    # 1. Preferred path: our JSON envelope.
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("__codec__") == _ENVELOPE_MARKER:
            return _from_envelope(obj)
    except (UnicodeDecodeError, ValueError):
        pass  # not our JSON — fall through to legacy pickle

    # 2. Legacy path: pre-migration pickle bytes.
    try:
        state = pickle.loads(raw)
        logger.info(
            "session_codec: read legacy pickle session — "
            "will be rewritten as JSON on next save"
        )
        return state
    except Exception as e:
        logger.error(f"session_codec: could not decode session payload ({e})")
        return None


def _from_envelope(obj: dict) -> Optional[ConversationState]:
    version = int(obj.get("__version__", 1))
    data = obj.get("data") or {}

    while version < SCHEMA_VERSION:
        migrate = _MIGRATIONS.get(version)
        if not migrate:
            break
        data = migrate(data)
        version += 1

    try:
        return _build_state(data)
    except Exception as e:
        logger.error(f"session_codec: failed to rebuild ConversationState ({e})")
        return None


def _only_known(cls, d: dict) -> dict:
    """Keep only keys that are still fields on ``cls``."""
    known = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in d.items() if k in known}


def _build_state(data: dict) -> ConversationState:
    messages = [
        ConversationMessage(**_only_known(ConversationMessage, m))
        for m in (data.get("messages") or [])
    ]

    blueprint = None
    if data.get("blueprint"):
        blueprint = ProjectBlueprint(**_only_known(ProjectBlueprint, data["blueprint"]))

    kwargs = _only_known(ConversationState, data)
    kwargs["messages"] = messages
    kwargs["blueprint"] = blueprint
    kwargs["stage"] = ConversationStage(
        data.get("stage", ConversationStage.INITIAL.value)
    )

    return ConversationState(**kwargs)
