#!/bin/bash
# restore.sh: Restore Postgres and Qdrant from a specified backup

set -e

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <postgres_backup_file> <qdrant_snapshot_file>"
    exit 1
fi

PG_BACKUP="$1"
QDRANT_BACKUP="$2"

echo "Starting restore at $(date)"

# 1. Restore PostgreSQL
if [ -f "$PG_BACKUP" ]; then
    echo "Restoring PostgreSQL from $PG_BACKUP..."
    if command -v pg_restore >/dev/null 2>&1; then
        pg_restore --clean --no-owner -d "$DATABASE_URL" "$PG_BACKUP"
        echo "✓ PostgreSQL restore complete."
    else
        echo "❌ pg_restore not found! Cannot restore database."
        exit 1
    fi
else
    echo "❌ PostgreSQL backup file not found: $PG_BACKUP"
    exit 1
fi

# 2. Restore Qdrant
if [ -f "$QDRANT_BACKUP" ]; then
    QDRANT_HOST=${QDRANT_URL:-http://localhost:6333}
    SNAPSHOT_FILENAME=$(basename "$QDRANT_BACKUP")
    echo "Uploading and restoring Qdrant snapshot: $SNAPSHOT_FILENAME..."
    
    # Upload snapshot to Qdrant
    UPLOAD_RESPONSE=$(curl -s -F "snapshot=@$QDRANT_BACKUP" "$QDRANT_HOST/collections/db_rules/snapshots/upload")
    echo "Upload response: $UPLOAD_RESPONSE"

    # Recover from uploaded snapshot
    RECOVER_RESPONSE=$(curl -s -X POST "$QDRANT_HOST/collections/db_rules/snapshots/recover" \
        -H "Content-Type: application/json" \
        -d "{\"location\": \"http://localhost:6333/collections/db_rules/snapshots/$SNAPSHOT_FILENAME\"}")
    
    echo "Recover response: $RECOVER_RESPONSE"
    echo "✓ Qdrant restore complete."
else
    echo "❌ Qdrant snapshot file not found: $QDRANT_BACKUP"
    exit 1
fi

echo "Restore completed successfully at $(date)"
