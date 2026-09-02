# app/db/connection.py

from __future__ import annotations

import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

# Single shared instances. Heavy optional dependencies (qdrant-client, which
# pulls in a native grpc extension, and sentence-transformers) are imported
# lazily inside the functions below so that simply importing the app does not
# require them — keeps startup light and lets the test suite import the app
# in environments where those native wheels are unavailable/blocked.
_qdrant_client = None
_embedding_model = None


def get_qdrant_client():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=120,  # seconds — default is too short for large upserts
        )
        logger.info("✅ Qdrant client connected")
    return _qdrant_client


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(settings.EMBEDDING_MODEL)
        logger.info(f"✅ Embedding model loaded: {settings.EMBEDDING_MODEL}")
    return _embedding_model


def embed_text(text: str) -> list[float]:
    """Convert any text to a vector embedding."""
    model = get_embedding_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def ensure_collection_exists():
    """Create Qdrant collection if it doesn't exist yet."""
    from qdrant_client.models import Distance, VectorParams

    client = get_qdrant_client()
    existing = [c.name for c in client.get_collections().collections]

    if settings.QDRANT_COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=settings.EMBEDDING_DIMENSION,
                distance=Distance.COSINE,
            ),
        )
        logger.info(f"✅ Created Qdrant collection: {settings.QDRANT_COLLECTION_NAME}")
    else:
        logger.info(f"✅ Qdrant collection already exists: {settings.QDRANT_COLLECTION_NAME}")
