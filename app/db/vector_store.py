# app/db/vector_store.py

import json
import os
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from app.db.connection import get_qdrant_client, embed_text, ensure_collection_exists
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

RULES_FILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "rules", "rules.json"
)


def load_rules_from_file() -> list[dict]:
    """Load all rules from rules.json."""
    with open(RULES_FILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["rules"]


def rule_to_embedding_text(rule: dict) -> str:
    """
    Convert a rule dict into a rich text string for embedding.
    The richer this text, the better the vector search retrieval.
    """
    parts = []

    parts.append(f"Rule {rule['rule_id']}: {rule['rule_name']}")
    parts.append(f"Category: {rule['category']}")
    parts.append(f"Priority: {rule['priority']}")

    if rule.get("reason"):
        parts.append(f"Reason: {rule['reason']}")

    if rule.get("trigger_when"):
        parts.append("Use this rule when: " + ". ".join(rule["trigger_when"]))

    if rule.get("enforce"):
        parts.append("Enforce: " + ". ".join(rule["enforce"]))

    if rule.get("avoid"):
        parts.append("Avoid: " + ". ".join(rule["avoid"]))

    if rule.get("tags"):
        parts.append("Tags: " + ", ".join(rule["tags"]))

    if rule.get("examples"):
        ex = rule["examples"]
        if ex.get("valid"):
            parts.append("Valid examples: " + ", ".join(ex["valid"]))
        if ex.get("invalid"):
            parts.append("Invalid examples: " + ", ".join(ex["invalid"]))

    return "\n".join(parts)


UPSERT_BATCH_SIZE = 10  # keep payloads small to avoid write timeouts


def _upsert_with_retry(client, collection_name: str, batch: list, max_retries: int = 3):
    """Upsert a batch with exponential-backoff retries on timeout."""
    import time
    for attempt in range(1, max_retries + 1):
        try:
            client.upsert(collection_name=collection_name, points=batch)
            return
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt  # 2s, 4s, 8s
            logger.warning(f"  ⚠️  Upsert attempt {attempt} failed ({e}). Retrying in {wait}s…")
            time.sleep(wait)


def seed_rules_to_qdrant():
    """
    One-time setup: embed all rules and push into Qdrant in small batches.
    Run this once when setting up the project.
    Safe to re-run — it overwrites existing points with same IDs.
    """
    ensure_collection_exists()
    client = get_qdrant_client()
    rules = load_rules_from_file()

    points = []
    for rule in rules:
        text = rule_to_embedding_text(rule)
        vector = embed_text(text)

        point = PointStruct(
            id=rule["rule_id"],
            vector=vector,
            payload={
                "rule_id": rule["rule_id"],
                "rule_name": rule["rule_name"],
                "category": rule["category"],
                "priority": rule["priority"],
                "trigger_when": rule.get("trigger_when", []),
                "enforce": rule.get("enforce", []),
                "avoid": rule.get("avoid", []),
                "reason": rule.get("reason", ""),
                "tags": rule.get("tags", []),
                "examples": rule.get("examples", {}),
                "status_mapping": rule.get("status_mapping", {}),
                "embedding_text": text,
            },
        )
        points.append(point)
        logger.info(f"  Prepared rule #{rule['rule_id']}: {rule['rule_name']}")

    # Upload in small batches with retries
    total = 0
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        _upsert_with_retry(client, settings.QDRANT_COLLECTION_NAME, batch)
        total += len(batch)
        logger.info(f"  ✅ Uploaded batch {i // UPSERT_BATCH_SIZE + 1}: {total}/{len(points)} rules")

    logger.info(f"✅ Seeded {total} rules into Qdrant collection '{settings.QDRANT_COLLECTION_NAME}'")
    return total


def search_rules(query: str, top_k: int = None, category_filter: str = None) -> list[dict]:
    """
    Search for relevant rules given a user query.
    Returns top_k most relevant rules as dicts.
    """
    from qdrant_client.models import QueryRequest

    client = get_qdrant_client()
    k = top_k or settings.TOP_K_RULES

    query_vector = embed_text(query)

    # Optional: filter by category
    search_filter = None
    if category_filter:
        search_filter = Filter(
            must=[
                FieldCondition(
                    key="category",
                    match=MatchValue(value=category_filter)
                )
            ]
        )

    # ── new API (qdrant-client >= 1.7) ──────────────────────────
    results = client.query_points(
        collection_name=settings.QDRANT_COLLECTION_NAME,
        query=query_vector,
        limit=k,
        query_filter=search_filter,
        with_payload=True,
    ).points
    # ────────────────────────────────────────────────────────────

    rules = []
    for result in results:
        rule = result.payload
        rule["relevance_score"] = round(result.score, 4)
        rules.append(rule)

    return rules


def get_rules_by_ids(rule_ids: list[int]) -> list[dict]:
    """Fetch specific rules by their IDs directly."""
    client = get_qdrant_client()

    # ── new API ──────────────────────────────────────────────────
    results = client.retrieve(
        collection_name=settings.QDRANT_COLLECTION_NAME,
        ids=rule_ids,
        with_payload=True,
    )
    # ────────────────────────────────────────────────────────────

    return [r.payload for r in results]


def get_collection_info() -> dict:
    """Get stats about the Qdrant collection."""
    client = get_qdrant_client()
    info = client.get_collection(settings.QDRANT_COLLECTION_NAME)

    # ── handle both old and new qdrant response structure ────────
    vectors_config = info.config.params.vectors
    if hasattr(vectors_config, 'size'):
        vector_size = vectors_config.size
        distance = str(vectors_config.distance)
    else:
        # newer qdrant returns a dict
        vector_size = settings.EMBEDDING_DIMENSION
        distance = "Cosine"
    # ────────────────────────────────────────────────────────────

    return {
        "collection_name": settings.QDRANT_COLLECTION_NAME,
        "total_rules": info.points_count,
        "vector_size": vector_size,
        "distance": distance,
        "status": str(info.status),
    }