#!/bin/bash
# backup.sh: Automated backup script for Postgres and Qdrant

set -e

BACKUP_DIR="/app/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

echo "Starting backup at $(date)"

# 1. PostgreSQL Backup
if [ -n "$DATABASE_URL" ]; then
    echo "Backing up PostgreSQL database..."
    if command -v pg_dump >/dev/null 2>&1; then
        pg_dump "$DATABASE_URL" -F c -b -v -f "$BACKUP_DIR/postgres_$TIMESTAMP.dump"
        echo "✓ PostgreSQL backup saved to $BACKUP_DIR/postgres_$TIMESTAMP.dump"
    else
        echo "⚠️ pg_dump not found! Skipping database dump."
    fi
fi

# 2. Qdrant Snapshot
QDRANT_HOST=${QDRANT_URL:-http://localhost:6333}
echo "Triggering Qdrant snapshot for collection 'db_rules'..."
SNAPSHOT_RESPONSE=$(curl -s -X POST "$QDRANT_HOST/collections/db_rules/snapshots")

if echo "$SNAPSHOT_RESPONSE" | grep -q '"status":"ok"'; then
    SNAPSHOT_NAME=$(echo "$SNAPSHOT_RESPONSE" | grep -o '"name":"[^"]*' | head -n 1 | cut -d'"' -f4)
    echo "✓ Qdrant snapshot created: $SNAPSHOT_NAME"
    echo "Downloading snapshot..."
    curl -s -o "$BACKUP_DIR/qdrant_db_rules_${TIMESTAMP}_${SNAPSHOT_NAME}" "$QDRANT_HOST/collections/db_rules/snapshots/$SNAPSHOT_NAME"
    echo "✓ Qdrant snapshot saved to $BACKUP_DIR/qdrant_db_rules_${TIMESTAMP}_${SNAPSHOT_NAME}"
else
    echo "❌ Qdrant snapshot failed: $SNAPSHOT_RESPONSE"
fi

echo "Backup completed successfully at $(date)"
