# app/services/job_store.py
"""
Redis-backed job store for async schema generation with in-memory fallback.

Design decisions:
- Primary persistence in Redis via `get_redis_client()` using key pattern `job:{job_id}` (24h TTL).
- Concurrent thread-safe fallback to memory dict if Redis is offline during local dev.
- Preserves identical interface (`create`, `mark_started`, `update_progress`, `complete`, `fail`, `get`, `exists`, `count`).
"""

import json
import uuid
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.session_store import get_redis_client

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 86400  # 24 hours


class _JobStore:
    def __init__(self):
        self._memory_jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _save_to_redis(self, job_id: str, data: dict):
        client = get_redis_client()
        if client:
            try:
                client.setex(f"job:{job_id}", JOB_TTL_SECONDS, json.dumps(data))
                return True
            except Exception as e:
                logger.error(f"Failed to save job {job_id[:8]} to Redis: {e}")
        return False

    def _get_from_redis(self, job_id: str) -> Optional[dict]:
        client = get_redis_client()
        if client:
            try:
                raw = client.get(f"job:{job_id}")
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.error(f"Failed to fetch job {job_id[:8]} from Redis: {e}")
        return None

    # ── Write ────────────────────────────────────────────────────

    def create(self, requirement: str, blueprint: Optional[dict], owner_id: Optional[str] = None, project_id: Optional[str] = None) -> str:
        """Register a new job and return its UUID."""
        job_id = str(uuid.uuid4())
        job_data = {
            "job_id":     job_id,
            "owner_id":   owner_id,
            "project_id": project_id,
            "status":     "queued",       # queued | generating | done | failed
            "requirement": requirement[:120] + ("..." if len(requirement) > 120 else ""),
            "progress": {
                "phase":           "queued",
                "current_module":  None,
                "modules_done":    0,
                "modules_total":   0,
                "tables_done":     0,
                "tables_planned":  0,
            },
            "result":       None,
            "error":        None,
            "created_at":   _now(),
            "started_at":   None,
            "completed_at": None,
        }

        with self._lock:
            self._memory_jobs[job_id] = job_data
        self._save_to_redis(job_id, job_data)

        return job_id

    def mark_started(self, job_id: str, modules_total: int, tables_planned: int):
        """Called when generation begins."""
        job = self.get(job_id)
        if job:
            job["status"] = "generating"
            job["started_at"] = _now()
            job["progress"].update({
                "phase":          "generating",
                "modules_total":  modules_total,
                "tables_planned": tables_planned,
            })
            with self._lock:
                self._memory_jobs[job_id] = job
            self._save_to_redis(job_id, job)

    def update_progress(
        self,
        job_id: str,
        *,
        current_module: str,
        modules_done: int,
        tables_done: int,
    ):
        """Called after each module batch completes."""
        job = self.get(job_id)
        if job:
            job["progress"].update({
                "current_module": current_module,
                "modules_done":   modules_done,
                "tables_done":    tables_done,
            })
            with self._lock:
                self._memory_jobs[job_id] = job
            self._save_to_redis(job_id, job)

    def complete(self, job_id: str, result: dict):
        """Store final result and mark job done."""
        job = self.get(job_id)
        if job:
            job["status"]       = "done"
            job["result"]       = result
            job["completed_at"] = _now()
            job["progress"]["phase"] = "done"
            with self._lock:
                self._memory_jobs[job_id] = job
            self._save_to_redis(job_id, job)

    def fail(self, job_id: str, error: str):
        """Mark job as failed."""
        job = self.get(job_id)
        if job:
            job["status"]       = "failed"
            job["error"]        = error
            job["completed_at"] = _now()
            job["progress"]["phase"] = "failed"
            with self._lock:
                self._memory_jobs[job_id] = job
            self._save_to_redis(job_id, job)

    # ── Read ─────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[dict]:
        """Fetch job data from Redis, falling back to local memory store."""
        data = self._get_from_redis(job_id)
        if data is not None:
            return data
        with self._lock:
            return dict(self._memory_jobs[job_id]) if job_id in self._memory_jobs else None

    def verify_ownership(self, job_id: str, user_id: Optional[str]) -> bool:
        """
        Verify if the requesting user owns the job.
        If the job has an owner_id and user_id is provided, verify matching IDs.
        If user_id is provided and job has a different owner_id, return False.
        Guest jobs (owner_id is None) remain accessible for backward compatibility.
        """
        job = self.get(job_id)
        if not job:
            return False
        job_owner = job.get("owner_id")
        if job_owner and user_id and job_owner != user_id:
            return False
        return True

    def exists(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def count(self) -> dict:
        client = get_redis_client()
        statuses = []
        if client:
            try:
                keys = client.keys("job:*")
                for k in keys:
                    raw = client.get(k)
                    if raw:
                        parsed = json.loads(raw)
                        statuses.append(parsed.get("status", "unknown"))
            except Exception as e:
                logger.error(f"Error fetching job keys from Redis: {e}")

        if not statuses:
            with self._lock:
                statuses = [j["status"] for j in self._memory_jobs.values()]

        return {s: statuses.count(s) for s in set(statuses)} if statuses else {}


# ── Singleton ────────────────────────────────────────────────────

_store_instance: Optional[_JobStore] = None
_store_lock = threading.Lock()


def get_job_store() -> _JobStore:
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = _JobStore()
    return _store_instance


# ── Helpers ──────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
