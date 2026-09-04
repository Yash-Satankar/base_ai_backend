# app/services/schema_refiner.py
"""
Auto-iteration schema refinement.

Sits on top of the generation pipeline as a post-generation stage:

    generate → structural validate → (quick fix pass) → **refine_until_clean** → done

Each iteration:
  1. run :class:`~app.validators.schema_validator.SchemaValidator` and
     :func:`~app.services.mysql_execution_validator.execute_and_validate`
     on the current DDL;
  2. if it is clean — no structural critical/high issues, MySQL accepts it, no
     enterprise-check *errors*, advisories at/below the threshold — stop;
  3. otherwise build a **concrete** fix instruction from the actual findings
     (verbatim MySQL error text + enterprise-check messages + structural rule
     violations — never "please improve this"), make ONE LLM call through the
     tagged ``llm_client`` (``operation="schema_refine"``, cost-attributed to
     the conversation), take the revised DDL, and loop.

Caps at ``max_iterations``. If it never converges, it returns the *best*
iteration seen (fewest weighted errors) with ``converged=False`` and
``remaining_issues`` listed honestly — it never presents an unfixed schema as
done.

Decision-B cost degrade: if the conversation is already degraded
(``llm_client.should_degrade``) the loop is capped to a single iteration.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.core.config import settings
from app.conversation import llm_client
from app.validators.schema_validator import SchemaValidator, ValidationResult

logger = logging.getLogger(__name__)

# Weight per finding when picking the "best" non-converged iteration and when
# deciding convergence. Lower total = better.
_STRUCT_WEIGHT = {"critical": 100, "high": 50, "medium": 8, "low": 1}
_DDL_ERROR_WEIGHT = 1000
_EXEC_ERROR_WEIGHT = 200
_EXEC_ADVISORY_WEIGHT = 5

_SYSTEM_FALLBACK = (
    "You are a senior MySQL database architect. You fix schemas so they run "
    "on MySQL 8 (InnoDB, utf8mb4) and satisfy enterprise conventions. You "
    "return raw SQL only."
)


@dataclass
class RefinementResult:
    final_ddl: str
    iterations_used: int                 # number of LLM fix calls actually made
    converged: bool
    remaining_issues: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    total_cost_usd: float = 0.0
    degraded: bool = False
    final_structural_score: int = 0
    final_execution: Optional[dict] = None   # serialised ExecutionResult of final_ddl

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        state = "converged" if self.converged else "did NOT converge"
        return (
            f"schema_refine: {state} after {self.iterations_used} iteration(s) "
            f"| {len(self.remaining_issues)} issue(s) remaining "
            f"| ${self.total_cost_usd:.4f}"
            f"{' | degraded (capped to 1)' if self.degraded else ''}"
        )


# ── validation assessment ──────────────────────────────────────────

def _assess_execution(ddl: str):
    """Run the MySQL execution validator, or ``None`` if it is disabled.
    Returns the ``ExecutionResult`` object (not the dict). Never raises."""
    if not settings.MYSQL_EXEC_VALIDATION_ENABLED:
        return None
    try:
        from app.services.mysql_execution_validator import execute_and_validate
        return execute_and_validate(ddl)
    except Exception as e:  # pragma: no cover - defensive; gate itself is guarded
        logger.error("schema_refine: execution validation raised (non-fatal): %s", e, exc_info=True)
        return None


def _structural_blocking(struct: ValidationResult) -> list:
    return [i for i in struct.issues if i.severity in ("critical", "high")]


def _error_weight(struct: ValidationResult, execu) -> int:
    w = sum(_STRUCT_WEIGHT.get(i.severity, 1) for i in struct.issues)
    if execu is not None and not execu.skipped:
        w += _DDL_ERROR_WEIGHT * len(execu.ddl_errors)
        w += _EXEC_ERROR_WEIGHT * len(execu.error_issues)
        w += _EXEC_ADVISORY_WEIGHT * len(execu.advisory_issues)
    return w


def _is_clean(struct: ValidationResult, execu, advisory_threshold: int) -> bool:
    if _structural_blocking(struct):
        return False
    if execu is not None and not execu.skipped:
        if not execu.success:
            return False
        if execu.error_issues:
            return False
        if len(execu.advisory_issues) > advisory_threshold:
            return False
    return True


def _remaining_issues(struct: ValidationResult, execu) -> list[dict]:
    """Flat, honest list of what is still wrong — the same shape whether it came
    from the structural validator or the live MySQL run."""
    out: list[dict] = []
    for i in _structural_blocking(struct):
        out.append({
            "source": "structural",
            "severity": i.severity,
            "category": f"rule-{i.rule_id}",
            "message": i.issue,
            "suggestion": i.suggestion,
            "table": i.table_name,
        })
    if execu is not None and not execu.skipped:
        for e in execu.ddl_errors:
            out.append({"source": "mysql", "severity": "error",
                        "category": "ddl-error", "message": e})
        for it in execu.issues:
            out.append({
                "source": "enterprise",
                "severity": it.severity,
                "category": it.category,
                "message": it.message,
                "table": it.table,
                "object": it.object_name,
            })
    return out


# ── fix-instruction builder ────────────────────────────────────────

_MAX_DDL_IN_PROMPT = 24000


def _build_fix_instruction(ddl: str, struct: ValidationResult, execu, requirement: str) -> str:
    sections: list[str] = []

    if execu is not None and not execu.skipped and execu.ddl_errors:
        lines = "\n".join(f"  - {e}" for e in execu.ddl_errors)
        sections.append(
            "## MySQL 8 REJECTED these statements — exact engine errors, fix every one:\n"
            f"{lines}"
        )

    if execu is not None and not execu.skipped:
        errs = [i for i in execu.issues if i.severity == "error"]
        advs = [i for i in execu.issues if i.severity == "advisory"]
        if errs:
            lines = "\n".join(
                f"  - [{i.category}] {i.message}" + (f"  (table: {i.table})" if i.table else "")
                for i in errs
            )
            sections.append("## Enterprise checks — ERRORS (must fix):\n" + lines)
        if advs:
            lines = "\n".join(
                f"  - [{i.category}] {i.message}" + (f"  (table: {i.table})" if i.table else "")
                for i in advs
            )
            sections.append("## Enterprise checks — advisories (fix where reasonable):\n" + lines)

    blocking = _structural_blocking(struct)
    if blocking:
        lines = "\n".join(
            f"  - [{i.severity}] {i.rule_name}: {i.issue}\n      → {i.suggestion}"
            for i in blocking
        )
        sections.append("## Structural rule violations (critical/high):\n" + lines)

    findings = "\n\n".join(sections) if sections else "(no machine-readable findings — tighten types, keys and indexes)"

    req = (requirement or "").strip()
    if len(req) > 1500:
        req = req[:1500] + " …"

    ddl_for_prompt = ddl if len(ddl) <= _MAX_DDL_IN_PROMPT else ddl[:_MAX_DDL_IN_PROMPT] + "\n-- … (truncated)"

    return (
        "The MySQL schema below failed validation. Apply the SMALLEST set of "
        "changes that resolves the findings.\n\n"
        "RULES:\n"
        "  - Return the COMPLETE corrected schema as ONE block of raw MySQL DDL. "
        "No prose, no markdown fences, no commentary.\n"
        "  - Do NOT drop or rename tables. Do NOT remove columns that are not "
        "part of a finding.\n"
        "  - Keep ENGINE=InnoDB and DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci "
        "on every table.\n"
        "  - Preserve the `SET FOREIGN_KEY_CHECKS = 0;` … `SET FOREIGN_KEY_CHECKS = 1;` "
        "wrapper if present.\n\n"
        f"{findings}\n\n"
        f"## Original requirement (context only):\n{req or '(not supplied)'}\n\n"
        f"## Current schema to fix:\n{ddl_for_prompt}\n"
    )


_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?', re.IGNORECASE
)

# A refinement iteration is rejected if it comes back with fewer than this
# fraction of the tables it was given — the model truncated or dropped tables
# instead of returning the whole corrected schema.
_MIN_TABLE_RETENTION = 0.95


def _table_names(ddl: str) -> set[str]:
    return {m.lower() for m in _CREATE_TABLE_RE.findall(ddl or "")}


def _extract_ddl(content: str) -> str:
    if not content:
        return ""
    text = content.strip()
    if "```" in text:
        # take the largest fenced block if the model wrapped it anyway
        blocks = re.findall(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if blocks:
            text = max(blocks, key=len)
    text = _FENCE_RE.sub("", text).strip()
    return text


# ── main entry point ───────────────────────────────────────────────

def refine_until_clean(
    ddl: str,
    requirement_context: dict,
    *,
    max_iterations: int = 3,
    advisory_threshold: Optional[int] = None,
    degraded: Optional[bool] = None,
) -> RefinementResult:
    """See module docstring.

    ``requirement_context`` keys (all optional except where noted):
      - ``requirement``   — the user's requirement text, for prompt context
      - ``system_prompt`` — the rules-aware system prompt from generation
      - ``session_id``    — conversation id, for cost attribution + degrade check
      - ``project_id``    — project id, for cost attribution
    """
    ctx = requirement_context or {}
    session_id = ctx.get("session_id")
    project_id = ctx.get("project_id")
    system_prompt = ctx.get("system_prompt") or _SYSTEM_FALLBACK
    requirement = ctx.get("requirement") or ""

    if advisory_threshold is None:
        advisory_threshold = settings.SCHEMA_REFINE_ADVISORY_THRESHOLD

    if degraded is None:
        degraded = llm_client.should_degrade(session_id)
    cap = 1 if degraded else max(0, max_iterations)

    current = ddl or ""
    history: list[dict] = []
    llm_calls = 0
    total_cost = 0.0
    best: Optional[dict] = None

    while True:
        struct = SchemaValidator().validate(current)
        execu = _assess_execution(current)
        weight = _error_weight(struct, execu)
        clean = _is_clean(struct, execu, advisory_threshold)

        exec_snapshot = None
        if execu is not None:
            exec_snapshot = {
                "skipped": execu.skipped,
                "success": execu.success,
                "ddl_error_count": len(execu.ddl_errors),
                "error_issue_count": len(execu.error_issues),
                "advisory_issue_count": len(execu.advisory_issues),
                "summary": execu.summary(),
            }
        history.append({
            "phase": "assess",
            "iteration": llm_calls,
            "error_weight": weight,
            "structural_score": struct.score,
            "structural_blocking": len(_structural_blocking(struct)),
            "execution": exec_snapshot,
            "clean": clean,
        })

        if best is None or weight < best["weight"]:
            best = {"weight": weight, "ddl": current, "struct": struct, "execu": execu}

        if clean:
            logger.info(
                "schema_refine: clean after %d iteration(s) (weight=%d)", llm_calls, weight
            )
            return RefinementResult(
                final_ddl=current,
                iterations_used=llm_calls,
                converged=True,
                remaining_issues=[],
                history=history,
                total_cost_usd=round(total_cost, 6),
                degraded=degraded,
                final_structural_score=struct.score,
                final_execution=execu.to_dict() if execu is not None else None,
            )

        if llm_calls >= cap:
            break

        prompt = _build_fix_instruction(current, struct, execu, requirement)
        try:
            resp = llm_client.call_llm(
                operation="schema_refine",
                system_prompt=system_prompt,
                user_prompt=prompt,
                session_id=session_id,
                project_id=project_id,
                max_tokens=settings.SCHEMA_REFINE_MAX_TOKENS,
                degrade=degraded,
            )
        except Exception as e:
            logger.error("schema_refine: LLM call failed on iteration %d: %s", llm_calls + 1, e)
            history.append({
                "phase": "refine", "iteration": llm_calls + 1,
                "error": str(e)[:200], "cost_usd": 0.0, "produced_ddl": False,
            })
            break

        llm_calls += 1
        cost = float(resp.get("cost_usd", 0.0) or 0.0)
        total_cost += cost
        revised = _extract_ddl(resp.get("content", ""))
        history.append({
            "phase": "refine",
            "iteration": llm_calls,
            "cost_usd": round(cost, 6),
            "model": resp.get("model"),
            "degraded": bool(resp.get("degraded")),
            "produced_ddl": bool(revised),
        })

        if not revised or revised.strip() == current.strip():
            logger.info(
                "schema_refine: iteration %d produced no usable change — stopping", llm_calls
            )
            break

        # Guard against the model returning a *shrunk* schema — a whole-schema
        # rewrite that dropped or truncated tables. Keep the pre-refine DDL and
        # stop; the caller's completeness gate is the final backstop.
        before, after = _table_names(current), _table_names(revised)
        if before and len(after) < _MIN_TABLE_RETENTION * len(before):
            dropped = sorted(before - after)
            logger.warning(
                "schema_refine: iteration %d dropped %d/%d tables (%s…) — "
                "discarding this revision and stopping",
                llm_calls, len(before) - len(after), len(before),
                ", ".join(dropped[:5]),
            )
            history[-1]["rejected_shrunk"] = {
                "before": len(before), "after": len(after),
                "dropped_sample": dropped[:10],
            }
            break

        current = revised

    # ── did not converge — return the best iteration, honestly ────
    b = best or {"ddl": current, "struct": SchemaValidator().validate(current), "execu": None}
    remaining = _remaining_issues(b["struct"], b["execu"])
    logger.warning(
        "schema_refine: did NOT converge after %d iteration(s); returning best "
        "iteration with %d issue(s) remaining", llm_calls, len(remaining)
    )
    return RefinementResult(
        final_ddl=b["ddl"],
        iterations_used=llm_calls,
        converged=False,
        remaining_issues=remaining,
        history=history,
        total_cost_usd=round(total_cost, 6),
        degraded=degraded,
        final_structural_score=b["struct"].score,
        final_execution=b["execu"].to_dict() if b["execu"] is not None else None,
    )
