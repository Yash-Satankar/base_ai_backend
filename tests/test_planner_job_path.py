"""
Regression tests for the async job path in
``app.services.planner_service.generate_database_schema_for_job``.

The job path has its own copy of the module/batch loop (separate from the
synchronous ``generate_database_schema``). A NameError in that loop —
``_clean_sql`` instead of ``clean_sql`` — went unnoticed for a long time
because every batch failure is swallowed by a broad ``except Exception`` and
only surfaces as ``modules_failed`` in the summary, never as a raise.

These tests drive the real function with the LLM / rule-matcher / fix-pass
stubbed, and assert the batch loop actually completes: batches succeed, the
markdown fences are stripped (i.e. ``clean_sql`` ran), and no module is marked
failed.
"""

import pytest

from app.services import planner_service as ps
from app.services.job_store import get_job_store


_FENCED_BATCH_SQL = """```sql
CREATE TABLE company_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE branch_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  company_id INT NOT NULL,
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```"""


@pytest.fixture
def stub_generation(monkeypatch):
    """Stub every external dependency of the job path except the batch loop
    itself (stitch / validate / clean_sql stay real)."""
    monkeypatch.setattr(ps, "match_rules", lambda _req: {
        "rules": [{"rule_id": 1, "rule_name": "R", "priority": "high", "category": "c"}],
        "primary_domain": "generic",
        "all_domains": ["generic"],
        "domain_confidence": 0.9,
        "semantic_matches": 1,
    })
    monkeypatch.setattr(ps, "build_system_prompt", lambda _rules: "SYS")
    monkeypatch.setattr(ps, "generate_schema", lambda **_kw: {
        "content": _FENCED_BATCH_SQL,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    })
    # keep the batch loop in focus — don't let the fix pass re-enter generate_schema
    monkeypatch.setattr(ps, "run_fix_pass", lambda sql, validation, _sp: (sql, validation))
    # execution gate stays off (default), but be explicit
    monkeypatch.setattr(ps, "run_execution_gate", lambda _sql: None)


BLUEPRINT = {
    "project_name": "JobPathProj",
    "gst_required": False,
    "scale": "medium",
    "modules": [
        {
            "name": "Core",
            "description": "core module",
            "tables": [
                {"name": "company_header_all", "purpose": "master company record", "columns": []},
                {"name": "branch_header_all", "purpose": "company branch record", "columns": []},
            ],
        }
    ],
}


def test_job_path_batch_loop_completes_and_strips_fences(stub_generation):
    store = get_job_store()
    job_id = store.create("build a company directory", BLUEPRINT)

    ps.generate_database_schema_for_job(
        job_id=job_id,
        requirement="build a company directory",
        blueprint=BLUEPRINT,
        session_id=None,
        mode="schema",
    )

    job = store.get(job_id)
    assert job["status"] == "done", job.get("error")

    result = job["result"]
    summary = result["generation_summary"]

    # The bug (`_clean_sql` NameError) made every batch fail here.
    assert summary["modules_failed"] == 0, result["generation_summary"]["failed_module_details"]
    assert summary["modules_succeeded"] == 1
    assert summary["tables_generated"] >= 2
    assert summary["is_complete"] is True

    schema = result["schema"]
    assert "company_header_all" in schema
    assert "branch_header_all" in schema
    # clean_sql actually ran on the job path — no markdown fence leaked through
    assert "```" not in schema


def test_job_path_reports_clean_sql_failure_as_module_failure(monkeypatch, stub_generation):
    """Guard-rail: if the batch loop's clean step ever raises again, it must
    show up as a failed module (not a silent empty schema that still says done).
    This is the signal the original bug produced — we assert it's detectable."""
    import app.services.planner_helpers as helpers

    def _boom(_sql):
        raise RuntimeError("simulated clean_sql regression")

    monkeypatch.setattr(ps, "clean_sql", _boom)

    store = get_job_store()
    job_id = store.create("build a company directory", BLUEPRINT)
    ps.generate_database_schema_for_job(
        job_id=job_id, requirement="build a company directory",
        blueprint=BLUEPRINT, session_id=None, mode="schema",
    )

    result = store.get(job_id)["result"]
    assert result["generation_summary"]["modules_failed"] == 1
    assert result["generation_summary"]["tables_generated"] == 0


# ── refinement stage wiring ──────────────────────────────────────────

def test_refinement_stage_runs_and_is_debug_gated(monkeypatch, stub_generation):
    """SCHEMA_REFINE_ENABLED → the job runs refine_until_clean, folds its
    result back into `combined_sql`/`validation`, and exposes the full
    refinement report in the (debug) job result — but NOT in the lean one."""
    from app.core.config import settings
    from app.services import schema_refiner
    from app.api.routes.planner import _lean_job_result

    monkeypatch.setattr(settings, "SCHEMA_REFINE_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEMA_REFINE_MAX_ITERATIONS", 2)

    seen = {}

    def fake_refine(ddl, ctx, *, max_iterations=3, **_kw):
        seen["ddl_in"] = ddl
        seen["ctx"] = ctx
        seen["max_iterations"] = max_iterations
        refined = ddl + "\n-- refined: added idx\n"
        return schema_refiner.RefinementResult(
            final_ddl=refined,
            iterations_used=2,
            converged=False,
            remaining_issues=[{"source": "enterprise", "severity": "advisory",
                               "category": "missing-timestamps", "message": "x_all has no timestamps"}],
            history=[{"phase": "assess", "iteration": 0, "clean": False},
                     {"phase": "refine", "iteration": 1, "cost_usd": 0.002},
                     {"phase": "refine", "iteration": 2, "cost_usd": 0.002}],
            total_cost_usd=0.004,
            degraded=False,
            final_structural_score=88,
            final_execution=None,
        )

    monkeypatch.setattr(schema_refiner, "refine_until_clean", fake_refine)

    store = get_job_store()
    job_id = store.create("build a company directory", BLUEPRINT)
    ps.generate_database_schema_for_job(
        job_id=job_id, requirement="build a company directory",
        blueprint=BLUEPRINT, session_id="conv-job-refine", mode="schema",
    )

    result = store.get(job_id)["result"]

    # it was actually invoked, with the job's context
    assert seen["max_iterations"] == 2
    assert seen["ctx"]["session_id"] == "conv-job-refine"
    assert seen["ctx"]["system_prompt"]  # rules-aware prompt was passed through

    # refined DDL replaced the generated one
    assert "-- refined: added idx" in result["schema"]

    # full report present in the debug result
    refine = result["validation"]["refinement"]
    assert refine["iterations_used"] == 2
    assert refine["converged"] is False
    assert refine["total_cost_usd"] == 0.004
    assert refine["remaining_issues"][0]["category"] == "missing-timestamps"
    assert refine["history"]

    # …and stripped from the lean (default, non-debug) response
    lean = _lean_job_result(result)
    assert "refinement" not in lean["validation"]


def test_refinement_stage_is_off_by_default(stub_generation):
    store = get_job_store()
    job_id = store.create("build a company directory", BLUEPRINT)
    ps.generate_database_schema_for_job(
        job_id=job_id, requirement="build a company directory",
        blueprint=BLUEPRINT, session_id=None, mode="schema",
    )
    result = store.get(job_id)["result"]
    assert result["validation"]["refinement"] is None


# ── batch retry + completeness gate ─────────────────────────────────
# Regression for the run where the async job silently shipped 9 of 36 planned
# tables (transient LLM errors dropped whole module batches) and still reported
# a converged, structurally-clean schema.

# 6 modules × 4 tables = 24 planned → exactly 6 generation batches at
# MAX_TABLES_PER_BATCH = 4.
BIG_BLUEPRINT = {
    "project_name": "BigProj",
    "gst_required": False,
    "scale": "medium",
    "modules": [
        {
            "name": f"Module{m}",
            "description": f"module {m}",
            "tables": [
                {"name": f"m{m}_t{t}_all", "purpose": f"table {t}", "columns": []}
                for t in range(4)
            ],
        }
        for m in range(6)
    ],
}
BIG_PLANNED = sum(len(mod["tables"]) for mod in BIG_BLUEPRINT["modules"])  # 24


class ScriptedGen:
    """Deterministic stand-in for ai_service.generate_schema, keyed by *logical
    batch* (1-based) rather than physical call, so retries behave naturally.

    - a successful batch emits 4 distinct `CREATE TABLE` statements
    - ``fail_batches``:  batch numbers that raise on every attempt
    - ``transient``:     {batch: k} raises for the first k attempts then succeeds
    - ``empty_batches``: batch numbers that return prose with no DDL every attempt
    """

    def __init__(self, fail_batches=(), transient=None, empty_batches=(), truncate_once=()):
        self.n = 0                     # total physical invocations
        self.batch = 0                 # current logical batch
        self._attempts_left = 0        # attempts remaining on the current batch
        self.fail_batches = set(fail_batches)
        self.transient = dict(transient or {})
        self.empty_batches = set(empty_batches)
        self.truncate_once = set(truncate_once)   # batch #: 1st attempt is truncated

    def _tables(self, truncated=False):
        blocks = [
            f"CREATE TABLE gen_b{self.batch}_{i}_all (id INT PRIMARY KEY, "
            f"created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
            for i in range(4)
        ]
        if truncated:                # chop the last statement mid-way
            blocks[-1] = (f"CREATE TABLE gen_b{self.batch}_3_all (id INT PRIMARY KEY, "
                          f"name VARCHAR(120) NOT NULL, note TEXT COMMENT 'x') "
                          f"ENGINE=InnoDB DEFAULT CHAR")
        return "\n".join(blocks)

    def __call__(self, *, system_prompt, user_prompt, max_tokens=None, **kw):
        self.n += 1
        if self._attempts_left == 0:          # a new batch begins
            self.batch += 1
            self._attempts_left = ps._BATCH_RETRY_ATTEMPTS + 1
            self._truncated_done = set()
        self._attempts_left -= 1

        if self.batch in self.fail_batches:
            raise RuntimeError(f"scripted permanent failure on batch {self.batch}")
        if self.transient.get(self.batch, 0) > 0:
            self.transient[self.batch] -= 1
            raise RuntimeError(f"scripted transient failure on batch {self.batch}")
        if self.batch in self.empty_batches:
            return {"content": "-- the model rambled and produced no schema --",
                    "usage": {"input_tokens": 100, "output_tokens": 5}}
        if self.batch in self.truncate_once and self.batch not in getattr(self, "_truncated_done", set()):
            self._truncated_done = getattr(self, "_truncated_done", set()) | {self.batch}
            return {"content": self._tables(truncated=True),
                    "usage": {"input_tokens": 200, "output_tokens": 400}}

        self._attempts_left = 0               # success → next call is a new batch
        return {"content": self._tables(), "usage": {"input_tokens": 200, "output_tokens": 400}}


@pytest.fixture
def stub_common(monkeypatch):
    """Stub everything the job path touches except the batch loop + gate."""
    monkeypatch.setattr(ps, "match_rules", lambda _req: {
        "rules": [{"rule_id": 1, "rule_name": "R", "priority": "high", "category": "c"}],
        "primary_domain": "generic", "all_domains": ["generic"],
        "domain_confidence": 0.9, "semantic_matches": 1,
    })
    monkeypatch.setattr(ps, "build_system_prompt", lambda _rules: "SYS")
    monkeypatch.setattr(ps, "build_module_prompt", lambda **_kw: "MODULE PROMPT")
    monkeypatch.setattr(ps, "run_fix_pass", lambda sql, val, _sp: (sql, val))
    monkeypatch.setattr(ps, "run_execution_gate", lambda _sql: None)
    # keep retries fast
    monkeypatch.setattr(ps, "_BATCH_RETRY_BACKOFF_SEC", 0.0)


def _run(blueprint):
    store = get_job_store()
    job_id = store.create("x", blueprint)
    ps.generate_database_schema_for_job(
        job_id=job_id, requirement="build the thing",
        blueprint=blueprint, session_id=None, mode="schema",
    )
    return store.get(job_id)


def test_transient_batch_failure_is_retried_and_recovers(stub_common, monkeypatch):
    # batch 2 fails once, batch 5 fails twice — both within the retry budget
    gen = ScriptedGen(transient={2: 1, 5: 2})
    monkeypatch.setattr(ps, "generate_schema", gen)

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "done", job.get("error")
    gs = job["result"]["generation_summary"]
    assert gs["tables_generated"] == BIG_PLANNED          # nothing lost
    assert gs["completeness_ratio"] == 1.0
    assert gs["modules_failed"] == 0
    assert gs["is_complete"] is True
    # 6 batches succeed, +1 retry (batch 2) +2 retries (batch 5) = 9 invocations
    assert gen.n == 9
    assert gen.batch == 6


def test_completeness_gate_fails_job_on_partial_schema(stub_common, monkeypatch):
    """The core regression: half the batches fail permanently → the job must
    FAIL, expose no result, and never reach the refiner."""
    from app.services import schema_refiner
    refiner_calls = []
    monkeypatch.setattr(schema_refiner, "refine_until_clean",
                        lambda *a, **k: refiner_calls.append(1))
    from app.core.config import settings
    monkeypatch.setattr(settings, "SCHEMA_REFINE_ENABLED", True)   # even enabled, must not run

    gen = ScriptedGen(fail_batches={2, 3, 4})   # 3 of 6 batches gone → 12/24 = 50%
    monkeypatch.setattr(ps, "generate_schema", gen)

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "failed"
    assert job["result"] is None                      # no fragment handed back
    assert "incomplete" in (job["error"] or "").lower()
    assert "12/24" in job["error"] or "50%" in job["error"]
    assert refiner_calls == []                        # refiner never saw it
    # 3 clean batches + 3 failing batches each retried the full number of times
    assert gen.n == 3 + 3 * (ps._BATCH_RETRY_ATTEMPTS + 1)


def test_completeness_gate_tolerates_minor_loss_but_flags_incomplete(stub_common, monkeypatch):
    gen = ScriptedGen(fail_batches={6})   # lose the last batch → 20/24 ≈ 83%
    monkeypatch.setattr(ps, "generate_schema", gen)
    from app.core.config import settings
    monkeypatch.setattr(settings, "SCHEMA_COMPLETENESS_MIN_RATIO", 0.80)  # 83% passes

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "done", job.get("error")
    gs = job["result"]["generation_summary"]
    assert gs["tables_generated"] == 20
    assert 0.80 <= gs["completeness_ratio"] < 0.85
    assert gs["modules_failed"] == 1
    assert gs["is_complete"] is False                 # flagged, not hidden


def test_completeness_min_ratio_is_config_driven(stub_common, monkeypatch):
    from app.core.config import settings

    # default 0.85 → 50% generation fails
    gen = ScriptedGen(fail_batches={4, 5, 6})
    monkeypatch.setattr(ps, "generate_schema", gen)
    monkeypatch.setattr(settings, "SCHEMA_COMPLETENESS_MIN_RATIO", 0.85)
    assert _run(BIG_BLUEPRINT)["status"] == "failed"

    # relax to 0.40 → the same generation now passes the gate
    gen2 = ScriptedGen(fail_batches={4, 5, 6})
    monkeypatch.setattr(ps, "generate_schema", gen2)
    monkeypatch.setattr(settings, "SCHEMA_COMPLETENESS_MIN_RATIO", 0.40)
    job = _run(BIG_BLUEPRINT)
    assert job["status"] == "done", job.get("error")
    assert job["result"]["generation_summary"]["completeness_ratio"] == 0.5


def test_empty_batch_responses_are_retried_then_fail_the_gate(stub_common, monkeypatch):
    """A truncated / no-DDL response must count as a failed batch, not a silent
    zero-table success."""
    gen = ScriptedGen(empty_batches={1, 2, 3, 4, 5, 6})   # every batch returns prose
    monkeypatch.setattr(ps, "generate_schema", gen)

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "failed"
    assert job["result"] is None
    assert "incomplete" in (job["error"] or "").lower()
    # every batch retried the full number of times
    assert gen.n == 6 * (ps._BATCH_RETRY_ATTEMPTS + 1)


def test_post_refinement_completeness_gate_fails_a_shrunk_schema(stub_common, monkeypatch):
    """Generation is complete (passes Step 3b), but the refiner returns a
    schema with tables dropped → the post-refinement gate must FAIL the job,
    not ship a converged-but-tiny schema."""
    from app.core.config import settings
    from app.services import schema_refiner

    monkeypatch.setattr(settings, "SCHEMA_REFINE_ENABLED", True)

    shrunk_ddl = "\n".join(
        f"CREATE TABLE kept_{i}_all (id INT PRIMARY KEY, created_on DATETIME NOT NULL, "
        f"modified_on DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        for i in range(5)                      # 5 tables vs 24 planned
    )

    def fake_refine(ddl, ctx, *, max_iterations=3, **_kw):
        return schema_refiner.RefinementResult(
            final_ddl=shrunk_ddl, iterations_used=3, converged=True,
            remaining_issues=[], history=[], total_cost_usd=0.01, degraded=False,
            final_structural_score=97, final_execution=None,
        )

    monkeypatch.setattr(schema_refiner, "refine_until_clean", fake_refine)

    gen = ScriptedGen()   # all 6 batches succeed → 24/24 generated
    monkeypatch.setattr(ps, "generate_schema", gen)

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "failed"
    assert job["result"] is None
    assert "incomplete after refinement" in (job["error"] or "").lower()
    assert "5/24" in job["error"]


def test_truncated_batch_is_retried_not_stitched(stub_common, monkeypatch):
    """A batch that returns 3 complete tables + 1 truncated one must be retried,
    not accepted — a corrupt CREATE TABLE cannot reach the stitched schema."""
    gen = ScriptedGen(truncate_once={3})     # batch 3's first attempt is truncated
    monkeypatch.setattr(ps, "generate_schema", gen)

    job = _run(BIG_BLUEPRINT)

    assert job["status"] == "done", job.get("error")
    gs = job["result"]["generation_summary"]
    assert gs["tables_generated"] == BIG_PLANNED       # 24 — nothing lost
    assert gs["modules_failed"] == 0
    # batch 3 was called twice (truncated, then clean); others once
    assert gen.n == 7
    schema = job["result"]["schema"]
    # every CREATE TABLE in the final schema is a complete, balanced statement
    from app.services.schema_refiner import _iter_table_blocks, _balanced
    for _n, _a, _b, block in _iter_table_blocks(schema):
        assert _balanced(block) and block.rstrip().endswith(";")
