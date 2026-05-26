# seed.py  ← run this once: python seed.py

import sys
import os
sys.path.append(os.path.dirname(__file__))

from app.db.vector_store import seed_rules_to_qdrant
from app.db.connection import ensure_collection_exists
import logging

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    print("🌱 Starting rules seeding into Qdrant...")
    count = seed_rules_to_qdrant()
    print(f"\n✅ Done. {count} rules are now searchable in Qdrant.")
    print("You can now start the server: uvicorn app.main:app --reload")