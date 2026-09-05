# app/services/schema_refiner.py
"""
Auto-iteration schema refinement — **targeted, per-table fixes**.

Sits on top of the generation pipeline as a post-generation stage:

    generate → structural validate → (quick fix pass) → refine_until_clean → done

Each iteration:
  1. run :class:`~app.validators.schema_validator.SchemaValidator` and
     :func:`~app.services.mysql_execution_validator.execute_and_validate`
     on the current (full) DDL;
  2. if it is clean — no structural critical/high issues, MySQL accepts it, no
     enterprise-check *errors*, advisories at/below the threshold — stop;
  3. otherwise **attribute** each finding to a specific table (MySQL engine
     errors name the offending statement; structural / enterprise findings
     carry a table or FK). Extract only those ``CREATE TABLE`` blocks (plus the
     parent blocks referenced by their FKs, read-only for type context), send
     just those to the LLM, splice the corrected blocks back into the full
     schema **byte-for-byte outside the touched tables**, and re-validate.
  4. If a finding cannot be attributed to a table (a genuinely schema-wide
     problem — e.g. a missing central registry table, cross-table charset
     drift), fall back to a whole-schema rewrite **for that iteration only**.

After every splice the result is checked for integrity — no table dropped,
duplicated, or corrupted outside the ones intentionally touched — and the
shrink-guard / completeness gate from the generation job still apply downstream.

Caps at ``max_iterations``. Never presents an unfixed schema as done: on
non-convergence it returns the best iteration with ``converged=False`` and
``remaining_issues`` listed honestly.

Decision-B cost degrade: a degraded conversation is capped to one iteration.
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

_STRUCT_WEIGHT = {"critical": 100, "high": 50, "medium": 8, "low": 1}
_DDL_ERROR_WEIGHT = 1000
_EXEC_ERROR_WEIGHT = 200
_EXEC_ADVISORY_WEIGHT = 5

# Cap tables touched per targeted call so the prompt — and, more importantly,
# the model's ability to actually comply with every individual finding —
# stays bounded. Empirically, cramming 40-60 simultaneously-broken tables
# into ONE call (tried at cap=60) does not converge even across
# SCHEMA_REFINE_MAX_ITERATIONS=5: the model reliably drops a large fraction
# of "add this to every FK" style fixes once the ask spans that many tables
# at once, no matter how many iterations are budgeted. A smaller per-call
# cap with a multi-iteration budget (attribution re-surfaces whatever is
# still broken each pass) converges more reliably than one giant sweep.
_MAX_TARGET_TABLES = 20
# Reject a whole-schema rewrite that comes back with fewer than this fraction
# of the tables it was given.
_MIN_TABLE_RETENTION = 0.95

_SYSTEM_FALLBACK = (
    "You are a senior MySQL database architect. You fix schemas so they run "
    "on MySQL 8 (InnoDB, utf8mb4) and satisfy enterprise conventions. You "
    "return raw SQL only."
)


@dataclass
class RefinementResult:
    final_ddl: str
    iterations_used: int
    converged: bool
    remaining_issues: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    total_cost_usd: float = 0.0
    degraded: bool = False
    final_structural_score: int = 0
    final_execution: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        state = "converged" if self.converged else "did NOT converge"
        modes = [h.get("mode") for h in self.history if h.get("phase") == "refine"]
        mode_str = ("/".join(dict.fromkeys(m for m in modes if m))) or "-"
        return (
            f"schema_refine: {state} after {self.iterations_used} iteration(s) "
            f"[{mode_str}] | {len(self.remaining_issues)} issue(s) remaining "
            f"| ${self.total_cost_usd:.4f}"
            f"{' | degraded (capped to 1)' if self.degraded else ''}"
        )


# ── SQL block parsing (paren- and string-aware) ────────────────────

_CREATE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?\s*\(', re.IGNORECASE
)
_ALTER_FK_RE = re.compile(
    r'ALTER\s+TABLE\s+[`"]?(\w+)[`"]?\s+ADD\s+(?:CONSTRAINT\s+[`"]?\w+[`"]?\s+)?'
    r'FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+[`"]?(\w+)[`"]?', re.IGNORECASE,
)


def _skip_string(s: str, i: int) -> int:
    """``i`` points at a quote char; return index just past the closing quote."""
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == q:
            return i + 1
        i += 1
    return i


_NEXT_STMT_RE = re.compile(
    r'\b(?:CREATE|ALTER|DROP|INSERT|COMMIT|START\s+TRANSACTION|SET)\b', re.IGNORECASE
)


def _stmt_end(s: str, start: int) -> int:
    """Index just past the terminating ``;`` for the statement beginning at
    ``start`` (string-aware). If a new top-level statement keyword appears
    before any ``;`` — a malformed / unterminated block — stop just before it
    so the damage is bounded rather than swallowing the rest of the file."""
    i = start
    while i < len(s):
        c = s[i]
        if c in "'\"`":
            j = _skip_string(s, i)
            if j == i:  # unterminated quote — bail out here
                return i
            i = j
            continue
        if c == ";":
            return i + 1
        if c == "\n" and _NEXT_STMT_RE.match(s[i + 1:].lstrip(" \t")):
            return i + 1
        i += 1
    return len(s)


def _iter_table_blocks(ddl: str):
    """Yield ``(name, start, end, text)`` for every CREATE TABLE — the span runs
    from ``CREATE`` to the statement's terminating ``;`` (including the
    ``ENGINE=… ;`` tail), paren- and string-aware so nested ``DECIMAL(10,2)`` /
    ``COMMENT '… ; …'`` do not confuse it."""
    for m in _CREATE_RE.finditer(ddl):
        name = m.group(1)
        i = m.end()          # just past the opening '('
        depth = 1
        while i < len(ddl) and depth:
            c = ddl[i]
            if c in "'\"`":
                i = _skip_string(ddl, i)
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        end = _stmt_end(ddl, i)
        yield name, m.start(), end, ddl[m.start():end]


def _table_block_map(ddl: str) -> dict[str, tuple[int, int, str]]:
    out: dict[str, tuple[int, int, str]] = {}
    for name, a, b, text in _iter_table_blocks(ddl):
        out[name.lower()] = (a, b, text)
    return out


def _table_names(ddl: str) -> set[str]:
    return {n.lower() for n, *_ in _iter_table_blocks(ddl)}


def _fk_parents(ddl: str) -> dict[str, set[str]]:
    """child table (lower) → set of parent tables (lower) it references, from
    both inline ``REFERENCES`` and ``ALTER TABLE … ADD … FOREIGN KEY``."""
    out: dict[str, set[str]] = {}
    for name, _a, _b, text in _iter_table_blocks(ddl):
        for pm in re.finditer(r'REFERENCES\s+[`"]?(\w+)[`"]?', text, re.IGNORECASE):
            out.setdefault(name.lower(), set()).add(pm.group(1).lower())
    for am in _ALTER_FK_RE.finditer(ddl):
        out.setdefault(am.group(1).lower(), set()).add(am.group(2).lower())
    return out


def _alter_fk_statements_for(ddl: str, tables: set[str]) -> list[str]:
    """Full ``ALTER TABLE … ADD … FOREIGN KEY`` statements whose altered table is
    in ``tables``."""
    stmts = []
    for am in _ALTER_FK_RE.finditer(ddl):
        if am.group(1).lower() in tables:
            end = _stmt_end(ddl, am.start())
            stmts.append(ddl[am.start():end].strip())
    return stmts


# ── validation assessment ──────────────────────────────────────────

def _assess_execution(ddl: str):
    if not settings.MYSQL_EXEC_VALIDATION_ENABLED:
        return None
    try:
        from app.services.mysql_execution_validator import execute_and_validate
        return execute_and_validate(ddl)
    except Exception as e:  # pragma: no cover
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


def _is_clean(struct: ValidationResult, execu, advisory_threshold: int, min_score: int = 0) -> bool:
    if _structural_blocking(struct):
        return False
    if struct.score < min_score:
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
    """Everything still wrong with the best iteration — not just the
    critical/high findings that BLOCK convergence. When a schema fails to
    converge purely on SCHEMA_REFINE_MIN_SCORE (medium/low findings dragging
    the score down with zero blocking issues), those findings are the ONLY
    reason for non-convergence and must still show up here, or the report is
    silently empty while the schema is reported as unclean."""
    out: list[dict] = []
    for i in struct.issues:
        out.append({
            "source": "structural", "severity": i.severity,
            "category": f"rule-{i.rule_id}", "message": i.issue,
            "suggestion": i.suggestion, "table": i.table_name,
        })
    if execu is not None and not execu.skipped:
        for e in execu.ddl_errors:
            out.append({"source": "mysql", "severity": "error",
                        "category": "ddl-error", "message": e})
        for it in execu.issues:
            out.append({
                "source": "enterprise", "severity": it.severity,
                "category": it.category, "message": it.message,
                "table": it.table, "object": it.object_name,
            })
    return out


# ── issue → table attribution ─────────────────────────────────────

_DDL_ERR_AT_RE = re.compile(r"\(at:\s*(.*?)\)\s*$", re.IGNORECASE | re.DOTALL)
_DDL_ERR_TABLE_RE = re.compile(
    r'(?:CREATE|ALTER)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?', re.IGNORECASE
)
# A failing statement isn't always CREATE/ALTER TABLE — a standalone
# `CREATE INDEX <name> ON <table> (...)` (e.g. a duplicate key name colliding
# with one already defined inline in that table) fails on its own statement.
# Attribute it to the table it targets so it's still an editable fix target,
# not schema-wide.
_DDL_ERR_INDEX_TABLE_RE = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+\S+\s+ON\s+[`"]?(\w+)[`"]?', re.IGNORECASE
)
_MISSING_TABLE_RE = re.compile(r"missing\s+'?([a-z0-9_]+)'?\s+table", re.IGNORECASE)
_COMPANION_ARCHIVE_RE = re.compile(r"has no Layer 3 archive table '([a-z0-9_]+)'", re.IGNORECASE)
_COMPANION_LIFECYCLE_RE = re.compile(r"has no '([a-z0-9_]+)' status-transition trail", re.IGNORECASE)
_TXN_NAME_RE = re.compile(r"(transaction|_txn|ledger|voucher|invoice|payment)", re.IGNORECASE)
# MySQL 1822: "Failed to add the foreign key constraint. Missing index for
# constraint '<fk>' in the referenced table '<parent>'" — the DDL error's
# "(at: CREATE TABLE <child> ...)" clause names the CHILD being created, but
# the actual fix (add an index/key on the referenced column) belongs on the
# PARENT, which must become an editable target, not read-only FK context.
_FK_MISSING_INDEX_RE = re.compile(
    r"Missing index for constraint '([^']+)' in the referenced table '([a-z0-9_]+)'",
    re.IGNORECASE,
)
# MySQL 3780: "Referencing column 'X' and referenced column 'Y' in foreign
# key constraint 'fk_name' are incompatible" — under SET FOREIGN_KEY_CHECKS=0,
# a FK that forward-references a not-yet-created table is deferred and only
# resolved once the parent table is finally created; MySQL then reports the
# error against whatever CREATE TABLE happens to be executing at that
# moment (the parent), not the child table that actually declares the
# mismatched column and needs the fix. The "(at: ...)" clause is misleading
# for this specific error class — re-attribute to whichever table's own
# CONSTRAINT clause actually names the failing constraint.
_FK_TYPE_INCOMPATIBLE_RE = re.compile(
    r"[Rr]eferencing column '([^']+)' and referenced column '([^']+)' in foreign "
    r"key constraint '([^']+)' are incompatible",
)
_CONSTRAINT_DEF_RE = re.compile(r"CONSTRAINT\s+[`\"]?(\w+)[`\"]?\s+FOREIGN\s+KEY", re.IGNORECASE)


def _table_declaring_constraint(ddl: str, constraint_name: str) -> Optional[str]:
    """Which table's own CREATE TABLE block declares a CONSTRAINT with this
    name — as opposed to whichever table a deferred-FK error happens to be
    reported against (see _FK_TYPE_INCOMPATIBLE_RE)."""
    for name, _a, _b, text in _iter_table_blocks(ddl):
        for m in _CONSTRAINT_DEF_RE.finditer(text):
            if m.group(1).lower() == constraint_name.lower():
                return name.lower()
    return None


@dataclass
class _Attribution:
    targeted: dict            # table_lower -> list[str] (human issue lines)
    missing: set              # tables the schema is told to add
    schemawide: list[dict]    # issues with no table home


def _attribute(struct: ValidationResult, execu, table_names: set[str],
               ddl: str = "") -> _Attribution:
    targeted: dict[str, list[str]] = {}
    missing: set[str] = set()
    schemawide: list[dict] = []

    def add(tbl: Optional[str], line: str):
        if tbl and tbl.lower() in table_names:
            targeted.setdefault(tbl.lower(), []).append(line)
            return True
        return False

    # structural — ALL severities, not just critical/high: medium/low findings
    # (anonymous indexes, missing archive/lifecycle companions, status-column
    # comments …) still cost real structural score even though they never
    # block convergence on their own.
    for i in struct.issues:
        line = f"[{i.severity}] {i.rule_name}: {i.issue} → {i.suggestion}"
        # Companion-table nudges name an EXISTING table (e.g. the header table
        # itself) in `table_name`, but the fix is a NEW sibling table — route
        # on the regex before the generic add() below, which would otherwise
        # attribute the finding to the header table and never ask for the
        # archive/lifecycle table to be created.
        ct = _COMPANION_ARCHIVE_RE.search(i.issue or "") or _COMPANION_LIFECYCLE_RE.search(i.issue or "")
        if ct:
            missing.add(ct.group(1).lower())
            continue
        if add(i.table_name, line):
            continue
        mt = _MISSING_TABLE_RE.search(i.issue or "")
        if mt:
            missing.add(mt.group(1).lower())
            continue
        # GST / running-balance rules name no table but target transaction tables
        if i.rule_id in (7, 27, 51, 57) or "transaction table" in (i.issue or "").lower():
            txn = [t for t in table_names if _TXN_NAME_RE.search(t)]
            if txn:
                for t in txn[:_MAX_TARGET_TABLES]:
                    targeted.setdefault(t, []).append(line)
                continue
        schemawide.append({"source": "structural", "severity": i.severity,
                           "message": i.issue, "suggestion": i.suggestion})

    if execu is not None and not execu.skipped:
        for e in execu.ddl_errors:
            m = _DDL_ERR_AT_RE.search(e)
            tbl = None
            if m:
                tm = _DDL_ERR_TABLE_RE.search(m.group(1)) or _DDL_ERR_INDEX_TABLE_RE.search(m.group(1))
                if tm:
                    tbl = tm.group(1)
            type_mismatch = _FK_TYPE_INCOMPATIBLE_RE.search(e)
            if type_mismatch and ddl:
                _child_col, _parent_col, constraint_name = type_mismatch.groups()
                declaring_table = _table_declaring_constraint(ddl, constraint_name)
                if declaring_table:
                    tbl = declaring_table
            fk_missing = _FK_MISSING_INDEX_RE.search(e)
            if fk_missing:
                fk_name, parent_tbl = fk_missing.groups()
                add(parent_tbl,
                    f"[MySQL engine error] This table is the REFERENCED parent of "
                    f"foreign key '{fk_name}' (from '{tbl or 'another table'}') but "
                    f"has no index on the referenced column(s) — add a KEY/INDEX "
                    f"(or make it the PRIMARY/UNIQUE key) covering those column(s) "
                    f"so the FK can be created.")
            if not add(tbl, f"[MySQL engine error] {e}"):
                schemawide.append({"source": "mysql", "severity": "error", "message": e})
        for it in execu.issues:
            line = f"[{it.severity}/{it.category}] {it.message}"
            if add(it.table, line):
                continue
            # charset-inconsistency etc. sometimes list tables in the message
            named = [t for t in table_names if re.search(rf"\b{re.escape(t)}\b", it.message or "")]
            if named:
                for t in named[:_MAX_TARGET_TABLES]:
                    targeted.setdefault(t, []).append(line)
                continue
            schemawide.append({"source": "enterprise", "severity": it.severity,
                               "category": it.category, "message": it.message})

    return _Attribution(targeted=targeted, missing=missing, schemawide=schemawide)


# ── prompt builders ───────────────────────────────────────────────

_MAX_DDL_IN_PROMPT = 24000


def _targeted_prompt(target_blocks: dict[str, str], context_blocks: dict[str, str],
                     alter_fks: list[str], missing: set[str],
                     issues_by_table: dict[str, list[str]], requirement: str) -> str:
    parts: list[str] = []
    for name, lines in issues_by_table.items():
        parts.append(f"### {name}\n" + "\n".join(f"  - {ln}" for ln in lines))
    if missing:
        parts.append("### (missing tables to ADD)\n" + "\n".join(f"  - {m}" for m in sorted(missing)))
    findings = "\n\n".join(parts)

    ctx = ""
    if context_blocks:
        ctx = ("\n\n## Parent tables (READ-ONLY, for column-type reference — do "
               "NOT return these):\n" + "\n\n".join(context_blocks.values()))
    fk = ("\n\n## Related FOREIGN KEY statements you may also correct:\n"
          + "\n".join(alter_fks)) if alter_fks else ""

    req = (requirement or "").strip()
    if len(req) > 1200:
        req = req[:1200] + " …"

    want = sorted(set(target_blocks) | set(missing))
    return (
        "Fix ONLY the issues listed below, in ONLY these tables. Return raw "
        "MySQL DDL — the corrected `CREATE TABLE …;` statement for each of: "
        f"{', '.join(want)}.\n\n"
        "RULES:\n"
        "  - Return one complete `CREATE TABLE` statement per table, nothing else. "
        "No prose, no markdown fences, no other tables.\n"
        "  - Do NOT rename tables or columns that are not part of a finding. "
        "Keep all existing columns unless a finding says to change them.\n"
        "  - Every finding below must actually be fixed in your output — do not "
        "return the table unchanged. If a finding says to REMOVE something "
        "(a UNIQUE constraint, a column, an index), remove it — do not keep it "
        "'to be safe'. If it says to ADD something (an ON DELETE action, an "
        "index, a column), add it explicitly.\n"
        "  - If a KEY/INDEX/FOREIGN KEY clause references a column that is not "
        "defined as an actual column in that same CREATE TABLE, either add that "
        "column (matching its type from context if it's a FK) or remove the "
        "dangling KEY/INDEX/FOREIGN KEY clause — never leave a reference to a "
        "column that does not exist.\n"
        "  - If a finding says this table is the REFERENCED PARENT of a foreign "
        "key that is missing an index, add a KEY/INDEX (or PRIMARY/UNIQUE) on "
        "the column(s) the other table's FK points at — infer the column from "
        "the child table's FOREIGN KEY clause if it is shown to you below.\n"
        "  - If a finding is a 'Duplicate key name' error from a standalone "
        "`CREATE INDEX <name> ON <this table> (...)` statement elsewhere in "
        "the schema, rename or remove the INLINE index/key inside this "
        "table's own CREATE TABLE definition that shares that same name — do "
        "not just leave two definitions with an identical name.\n"
        "  - ENGINE=InnoDB, DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci "
        "on every table. Money = DECIMAL. Event timestamps = DATETIME. "
        "FK columns need an index and an explicit ON DELETE / ON UPDATE action.\n"
        "  - For a 'missing table', emit a complete new `CREATE TABLE` following "
        "the same conventions.\n\n"
        f"## Findings by table:\n{findings}\n"
        f"{ctx}{fk}\n\n"
        f"## Requirement (context only):\n{req or '(not supplied)'}\n\n"
        "## Tables to correct (return corrected versions of these):\n"
        + "\n\n".join(target_blocks.values())
    )


def _whole_prompt(ddl: str, struct: ValidationResult, execu, requirement: str) -> str:
    sections: list[str] = []
    if execu is not None and not execu.skipped and execu.ddl_errors:
        sections.append("## MySQL 8 REJECTED these statements — fix every one:\n"
                        + "\n".join(f"  - {e}" for e in execu.ddl_errors))
    if execu is not None and not execu.skipped:
        errs = [i for i in execu.issues if i.severity == "error"]
        advs = [i for i in execu.issues if i.severity == "advisory"]
        if errs:
            sections.append("## Enterprise checks — ERRORS (must fix):\n" + "\n".join(
                f"  - [{i.category}] {i.message}" + (f"  (table: {i.table})" if i.table else "")
                for i in errs))
        if advs:
            sections.append("## Enterprise checks — advisories:\n" + "\n".join(
                f"  - [{i.category}] {i.message}" + (f"  (table: {i.table})" if i.table else "")
                for i in advs))
    blocking = _structural_blocking(struct)
    if blocking:
        sections.append("## Structural rule violations (critical/high):\n" + "\n".join(
            f"  - [{i.severity}] {i.rule_name}: {i.issue}\n      → {i.suggestion}" for i in blocking))

    findings = "\n\n".join(sections) if sections else "(tighten types, keys and indexes)"
    req = (requirement or "").strip()
    if len(req) > 1500:
        req = req[:1500] + " …"
    ddl_for_prompt = ddl if len(ddl) <= _MAX_DDL_IN_PROMPT else ddl[:_MAX_DDL_IN_PROMPT] + "\n-- … (truncated)"
    return (
        "The MySQL schema below failed validation. Apply the SMALLEST set of "
        "changes that resolves the findings.\n\n"
        "RULES:\n"
        "  - Return the COMPLETE corrected schema as ONE block of raw MySQL DDL. "
        "No prose, no markdown fences.\n"
        "  - Do NOT drop or rename tables. Do NOT remove columns not in a finding.\n"
        "  - ENGINE=InnoDB, DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci everywhere.\n"
        "  - Preserve the `SET FOREIGN_KEY_CHECKS = 0;` … `= 1;` wrapper if present.\n\n"
        f"{findings}\n\n"
        f"## Original requirement (context only):\n{req or '(not supplied)'}\n\n"
        f"## Current schema to fix:\n{ddl_for_prompt}\n"
    )


# ── response parsing / splicing ───────────────────────────────────

_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(content: str) -> str:
    if not content:
        return ""
    text = content.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if blocks:
            text = max(blocks, key=len)
    return _FENCE_RE.sub("", text).strip()


def _blocks_from_response(content: str) -> dict[str, str]:
    """All well-formed CREATE TABLE statements in an LLM response, keyed by
    lowercase name. A block must have balanced parens and end at ``;``."""
    text = _strip_fences(content)
    out: dict[str, str] = {}
    for name, _a, _b, block in _iter_table_blocks(text):
        b = block.strip()
        if b.rstrip().endswith(";") and b.count("(") >= 1 and _balanced(b):
            out[name.lower()] = b if b.endswith(";") else b + ";"
    return out


def _balanced(block: str) -> bool:
    depth = 0
    i = 0
    while i < len(block):
        c = block[i]
        if c in "'\"`":
            i = _skip_string(block, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def _splice_tables(current: str, replacements: dict[str, str], missing_added: dict[str, str]) -> str:
    """Replace the char-span of each named table with its corrected block
    (untouched bytes are preserved exactly), then insert any newly added
    tables before the closing COMMIT."""
    spans = _table_block_map(current)
    edits = []
    for name, new_block in replacements.items():
        if name in spans:
            a, b, _ = spans[name]
            edits.append((a, b, new_block.strip()))
    result = current
    for a, b, new in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:a] + new + result[b:]

    if missing_added:
        addition = "\n\n" + "\n\n".join(v.strip() for v in missing_added.values()) + "\n"
        m = re.search(r"\n\s*COMMIT\s*;", result, re.IGNORECASE)
        if m:
            result = result[:m.start()] + addition + result[m.start():]
        else:
            result = result.rstrip() + addition
    return result


def _splice_integrity(before: str, after: str, touched: set[str], added: set[str]) -> tuple[bool, list[str]]:
    """Confirm the splice dropped/duplicated/corrupted nothing outside ``touched``
    (∪ ``added``)."""
    problems: list[str] = []
    before_map = _table_block_map(before)
    after_blocks = list(_iter_table_blocks(after))
    after_names = [n.lower() for n, *_ in after_blocks]

    dups = {n for n in after_names if after_names.count(n) > 1}
    if dups:
        problems.append(f"duplicate table(s) after splice: {sorted(dups)}")

    expected = set(before_map) | {a.lower() for a in added}
    got = set(after_names)
    if got - expected:
        problems.append(f"unexpected new table(s): {sorted(got - expected)}")
    if expected - got:
        problems.append(f"dropped table(s): {sorted(expected - got)}")

    after_map = _table_block_map(after)
    for name, (_a, _b, text_before) in before_map.items():
        if name in touched or name in {a.lower() for a in added}:
            continue
        if name in after_map and after_map[name][2] != text_before:
            problems.append(f"untouched table '{name}' changed")

    for name in touched:
        blk = after_map.get(name, (0, 0, ""))[2]
        if blk and not _balanced(blk):
            problems.append(f"corrected block '{name}' has unbalanced parens")

    return (not problems), problems


# ── main entry point ───────────────────────────────────────────────

def refine_until_clean(
    ddl: str,
    requirement_context: dict,
    *,
    max_iterations: int = 3,
    advisory_threshold: Optional[int] = None,
    min_score: Optional[int] = None,
    degraded: Optional[bool] = None,
) -> RefinementResult:
    ctx = requirement_context or {}
    session_id = ctx.get("session_id")
    project_id = ctx.get("project_id")
    system_prompt = ctx.get("system_prompt") or _SYSTEM_FALLBACK
    requirement = ctx.get("requirement") or ""
    high_criticality = bool(ctx.get("high_criticality", False))

    if advisory_threshold is None:
        advisory_threshold = settings.SCHEMA_REFINE_ADVISORY_THRESHOLD
    if min_score is None:
        min_score = settings.SCHEMA_REFINE_MIN_SCORE
    if degraded is None:
        degraded = llm_client.should_degrade(session_id)
    cap = 1 if degraded else max(0, max_iterations)

    current = ddl or ""
    history: list[dict] = []
    llm_calls = 0
    total_cost = 0.0
    best: Optional[dict] = None

    while True:
        struct = SchemaValidator().validate(current, high_criticality=high_criticality)
        execu = _assess_execution(current)
        weight = _error_weight(struct, execu)
        clean = _is_clean(struct, execu, advisory_threshold, min_score)

        exec_snapshot = None
        if execu is not None:
            exec_snapshot = {
                "skipped": execu.skipped, "success": execu.success,
                "ddl_error_count": len(execu.ddl_errors),
                "error_issue_count": len(execu.error_issues),
                "advisory_issue_count": len(execu.advisory_issues),
                "summary": execu.summary(),
            }
        history.append({
            "phase": "assess", "iteration": llm_calls, "error_weight": weight,
            "structural_score": struct.score,
            "structural_blocking": len(_structural_blocking(struct)),
            "table_count": len(_table_names(current)),
            "execution": exec_snapshot, "clean": clean,
        })
        if best is None or weight < best["weight"]:
            best = {"weight": weight, "ddl": current, "struct": struct, "execu": execu}

        if clean:
            logger.info("schema_refine: clean after %d iteration(s) (weight=%d)", llm_calls, weight)
            return RefinementResult(
                final_ddl=current, iterations_used=llm_calls, converged=True,
                remaining_issues=[], history=history,
                total_cost_usd=round(total_cost, 6), degraded=degraded,
                final_structural_score=struct.score,
                final_execution=execu.to_dict() if execu is not None else None,
            )
        if llm_calls >= cap:
            break

        all_names = _table_names(current)
        attr = _attribute(struct, execu, all_names, current)
        target_names = list(attr.targeted)[:_MAX_TARGET_TABLES]
        use_targeted = bool(target_names or attr.missing)

        if use_targeted:
            revised, applied_note = _do_targeted_iteration(
                current, target_names, attr, all_names, requirement,
                system_prompt, session_id, project_id, degraded,
            )
            llm_calls += 1
            total_cost += applied_note["cost_usd"]
            applied_note.update({"phase": "refine", "iteration": llm_calls})
            history.append(applied_note)

            # A targeted splice that failed integrity / returned nothing is not
            # fatal — fall back to a whole-schema rewrite for this iteration
            # (still bounded by ``cap``).
            if revised is None and llm_calls < cap and (
                applied_note.get("rejected_integrity") or not applied_note.get("produced_ddl")
            ):
                logger.info("schema_refine: targeted iteration failed — falling back to whole-schema")
                revised, w_note = _do_whole_iteration(
                    current, struct, execu, requirement,
                    system_prompt, session_id, project_id, degraded,
                )
                llm_calls += 1
                total_cost += w_note["cost_usd"]
                w_note.update({"phase": "refine", "iteration": llm_calls, "fallback_from": "targeted"})
                history.append(w_note)
        else:
            revised, applied_note = _do_whole_iteration(
                current, struct, execu, requirement,
                system_prompt, session_id, project_id, degraded,
            )
            llm_calls += 1
            total_cost += applied_note["cost_usd"]
            applied_note.update({"phase": "refine", "iteration": llm_calls})
            history.append(applied_note)

        if revised is None or revised.strip() == current.strip():
            logger.info("schema_refine: iteration %d produced no usable change — stopping", llm_calls)
            break
        current = revised

    b = best or {"ddl": current,
                 "struct": SchemaValidator().validate(current, high_criticality=high_criticality),
                 "execu": None}
    remaining = _remaining_issues(b["struct"], b["execu"])
    logger.warning(
        "schema_refine: did NOT converge after %d iteration(s); best has %d issue(s)",
        llm_calls, len(remaining),
    )
    return RefinementResult(
        final_ddl=b["ddl"], iterations_used=llm_calls, converged=False,
        remaining_issues=remaining, history=history,
        total_cost_usd=round(total_cost, 6), degraded=degraded,
        final_structural_score=b["struct"].score,
        final_execution=b["execu"].to_dict() if b["execu"] is not None else None,
    )


# ── Schema decomposition: per-schema-module refinement ─────────────
# docs/enterprise_standards_spec.md §2.2/§2.4. Additive — refine_until_clean
# above is completely untouched, so the default (non-decomposed) single-
# schema path is unaffected by this function's existence.

def refine_schemas_until_clean(
    schemas: dict[str, str],
    requirement_context: dict,
    *,
    max_iterations: int = 3,
    advisory_threshold: Optional[int] = None,
    min_score: Optional[int] = None,
    degraded: Optional[bool] = None,
) -> dict[str, "RefinementResult"]:
    """Refine each schema-module's DDL independently — the entry point for a
    decomposed (multi-schema) project.

    Each schema gets its own ``refine_until_clean()`` call against ONLY its
    own DDL text. This reuses the existing, already-tested per-table
    attribution/splice machinery unchanged rather than teaching it a new
    multi-schema mode: since each call only ever sees one schema's tables,
    a fix can never splice across a schema boundary, and two schemas
    legitimately sharing a table name can never collide (each is validated
    and spliced in complete isolation from the other).

    ``session_id`` in ``requirement_context`` is suffixed with the schema
    name per call so Decision-B cost-degrade and cost attribution are
    tracked per schema-module, not pooled across the whole project.
    """
    results: dict[str, RefinementResult] = {}
    base_session_id = (requirement_context or {}).get("session_id")
    for schema_name, ddl in schemas.items():
        ctx = dict(requirement_context or {})
        if base_session_id:
            ctx["session_id"] = f"{base_session_id}::{schema_name}"
        results[schema_name] = refine_until_clean(
            ddl, ctx,
            max_iterations=max_iterations,
            advisory_threshold=advisory_threshold,
            min_score=min_score,
            degraded=degraded,
        )
    return results


def _call(system_prompt, user_prompt, session_id, project_id, degraded) -> dict:
    return llm_client.call_llm(
        operation="schema_refine", system_prompt=system_prompt, user_prompt=user_prompt,
        session_id=session_id, project_id=project_id,
        max_tokens=settings.SCHEMA_REFINE_MAX_TOKENS, degrade=degraded,
    )


def _do_targeted_iteration(current, target_names, attr, all_names, requirement,
                           system_prompt, session_id, project_id, degraded):
    block_map = _table_block_map(current)
    target_blocks = {n: block_map[n][2] for n in target_names if n in block_map}

    parents = _fk_parents(current)
    context_names = set()
    for n in target_blocks:
        context_names |= (parents.get(n, set()) - set(target_blocks))
    context_blocks = {n: block_map[n][2] for n in context_names if n in block_map}
    alter_fks = _alter_fk_statements_for(current, set(target_blocks))

    prompt = _targeted_prompt(
        target_blocks, context_blocks, alter_fks, attr.missing,
        {n: attr.targeted[n] for n in target_names}, requirement,
    )
    note = {"mode": "targeted", "targets": sorted(target_blocks),
            "missing_requested": sorted(attr.missing),
            "schemawide_deferred": len(attr.schemawide)}
    try:
        resp = _call(system_prompt, prompt, session_id, project_id, degraded)
    except Exception as e:
        logger.error("schema_refine: targeted LLM call failed: %s", e)
        return None, {**note, "cost_usd": 0.0, "error": str(e)[:200], "produced_ddl": False}

    note["cost_usd"] = round(float(resp.get("cost_usd", 0.0) or 0.0), 6)
    note["model"] = resp.get("model")
    note["degraded"] = bool(resp.get("degraded"))

    returned = _blocks_from_response(resp.get("content", ""))
    note["returned_blocks"] = sorted(returned)
    replacements = {n: returned[n] for n in target_blocks if n in returned}
    added = {n: returned[n] for n in attr.missing if n in returned}
    if not replacements and not added:
        return None, {**note, "produced_ddl": False}

    spliced = _splice_tables(current, replacements, added)
    ok, problems = _splice_integrity(
        current, spliced, touched=set(replacements), added=set(added),
    )
    note["produced_ddl"] = True
    note["applied"] = sorted(replacements)
    note["added"] = sorted(added)
    if not ok:
        logger.warning("schema_refine: targeted splice failed integrity (%s) — discarding", problems)
        note["rejected_integrity"] = problems
        return None, note
    return spliced, note


def _do_whole_iteration(current, struct, execu, requirement,
                        system_prompt, session_id, project_id, degraded):
    prompt = _whole_prompt(current, struct, execu, requirement)
    note = {"mode": "whole"}
    try:
        resp = _call(system_prompt, prompt, session_id, project_id, degraded)
    except Exception as e:
        logger.error("schema_refine: whole-schema LLM call failed: %s", e)
        return None, {**note, "cost_usd": 0.0, "error": str(e)[:200], "produced_ddl": False}

    note["cost_usd"] = round(float(resp.get("cost_usd", 0.0) or 0.0), 6)
    note["model"] = resp.get("model")
    note["degraded"] = bool(resp.get("degraded"))
    revised = _strip_fences(resp.get("content", ""))
    note["produced_ddl"] = bool(revised)
    if not revised:
        return None, note

    before, after = _table_names(current), _table_names(revised)
    if before and len(after) < _MIN_TABLE_RETENTION * len(before):
        dropped = sorted(before - after)
        logger.warning(
            "schema_refine: whole rewrite dropped %d/%d tables (%s…) — discarding",
            len(before) - len(after), len(before), ", ".join(dropped[:5]),
        )
        note["rejected_shrunk"] = {"before": len(before), "after": len(after),
                                   "dropped_sample": dropped[:10]}
        return None, note
    return revised, note
