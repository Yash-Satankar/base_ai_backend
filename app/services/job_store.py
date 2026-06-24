# app/services/job_store.py
"""
Thread-safe in-memory job store for async schema generation.

Design decisions:
- Singleton pattern so all routes share the same store instance.
- threading.Lock instead of asyncio.Lock because background tasks
  run in a threadpool (asyncio.to_thread), not the event loop.
- Jobs are kept in memory; on restart they are lost.  Acceptable for
  MVP — Phase 3 will optionally persist to PostgreSQL.
"""

import uuid
import threading
from datetime import datetime, timezone
from typing import Optional


class _JobStore:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Write ────────────────────────────────────────────────────

    def create(self, requirement: str, blueprint: Optional[dict]) -> str:
        """Register a new job and return its UUID."""
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = {
                "job_id":     job_id,
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
        return job_id

    def mark_started(self, job_id: str, modules_total: int, tables_planned: int):
        """Called when generation begins."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "generating"
                job["started_at"] = _now()
                job["progress"].update({
                    "phase":          "generating",
                    "modules_total":  modules_total,
                    "tables_planned": tables_planned,
                })

    def update_progress(
        self,
        job_id: str,
        *,
        current_module: str,
        modules_done: int,
        tables_done: int,
    ):
        """Called after each module batch completes."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["progress"].update({
                    "current_module": current_module,
                    "modules_done":   modules_done,
                    "tables_done":    tables_done,
                })

    def complete(self, job_id: str, result: dict):
        """Store final result and mark job done."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"]       = "done"
                job["result"]       = result
                job["completed_at"] = _now()
                job["progress"]["phase"] = "done"

    def fail(self, job_id: str, error: str):
        """Mark job as failed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"]       = "failed"
                job["error"]        = error
                job["completed_at"] = _now()
                job["progress"]["phase"] = "failed"

    # ── Read ─────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs

    def count(self) -> dict:
        with self._lock:
            statuses = [j["status"] for j in self._jobs.values()]
        return {s: statuses.count(s) for s in set(statuses)}


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
