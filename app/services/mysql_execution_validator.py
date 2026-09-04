# app/services/mysql_execution_validator.py
"""
MySQL *execution* validation — a second, optional gate that runs AFTER the
structural :class:`~app.validators.schema_validator.SchemaValidator`.

Where ``SchemaValidator`` scores the DDL text against the rule library, this
module actually **runs** the DDL against a real MySQL 8 server and reports:

1. ``ddl_errors``  — verbatim engine errors (FK type mismatch, duplicate
   constraint names, invalid defaults, reserved-word collisions, bad charset …).
   Nothing is paraphrased: the MySQL errno and message are passed through.
2. ``issues``      — "enterprise-grade" findings gathered from the *live*
   ``information_schema`` after a successful load, each tagged
   ``severity="error"`` or ``severity="advisory"``.

The MySQL backend is resolved in this order:

* ``MYSQL_EXEC_VALIDATION_DSN``            — an existing server. A per-run
  scratch database is created and dropped; if the DSN user cannot
  ``CREATE DATABASE`` the run falls back to the DSN's own database and drops
  only the tables it created (serialise runs in that mode);
* an ephemeral ``testcontainers`` MySQL 8 container (needs a running Docker);
* otherwise the run is **skipped** (``skipped=True``) — never a hard failure.

It is gated behind ``MYSQL_EXEC_VALIDATION_ENABLED`` (default off) because a real
DB spin-up is slow relative to a normal request.

The enterprise-check thresholds are not guessed — they are mined from the ~34
real production dumps in ``Databases/`` by ``scripts/mine_reference_dumps.py``;
the numbers quoted in the findings come from
``app/validators/reference_thresholds.json``. See ``scripts/reference_findings.md``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Literal, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

Severity = Literal["error", "advisory"]

_THRESHOLDS_PATH = Path(__file__).resolve().parent.parent / "validators" / "reference_thresholds.json"


def _load_thresholds() -> dict:
    try:
        return json.loads(_THRESHOLDS_PATH.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - only if the mined file is missing
        return {"derived_thresholds": {}, "foreign_keys": {}, "charset": {}, "timestamps": {}}


_TH = _load_thresholds()
_DT = _TH.get("derived_thresholds", {})

# Column-name families that count as an audit timestamp (mirrors the miner).
_CREATED_COLS = ("created_on", "created_at", "created_date", "createddate",
                 "date_created", "added_on", "entry_date", "createdon")
_UPDATED_COLS = ("modified_on", "updated_at", "updated_on", "modified_at",
                 "last_updated", "modifiedon", "updatedon")

_LEGACY_CHARSETS = ("latin1", "utf8mb3", "utf8", "swe7", "ascii")

_SCORE_PENALTY = {"error": 15, "advisory": 4}


# ── result types ────────────────────────────────────────────────────

@dataclass
class ExecutionIssue:
    severity: Severity
    category: str                 # kebab-case slug, e.g. "fk-no-referential-action"
    message: str
    table: Optional[str] = None
    object_name: Optional[str] = None   # constraint / index / column
    mysql_errno: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionResult:
    success: bool                          # executed AND zero ddl_errors
    executed: bool                         # DDL actually reached a real MySQL
    skipped: bool = False                  # no backend available — not a failure
    skip_reason: Optional[str] = None
    backend: Optional[str] = None          # "dsn" | "testcontainers"
    mysql_version: Optional[str] = None
    ddl_errors: list[str] = field(default_factory=list)
    issues: list[ExecutionIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables_created: list[str] = field(default_factory=list)
    statements_run: int = 0
    statements_failed: int = 0
    enterprise_score: int = 0             # 0-100, live-schema quality
    duration_seconds: float = 0.0

    @property
    def error_issues(self) -> list[ExecutionIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def advisory_issues(self) -> list[ExecutionIssue]:
        return [i for i in self.issues if i.severity == "advisory"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        d["error_issue_count"] = len(self.error_issues)
        d["advisory_issue_count"] = len(self.advisory_issues)
        d["summary"] = self.summary()
        return d

    def summary(self) -> str:
        if self.skipped:
            return f"⏭  MySQL execution validation skipped — {self.skip_reason}"
        status = "✅ executes cleanly" if self.success else "❌ MySQL rejected the DDL"
        return (
            f"{status} | {self.statements_run - self.statements_failed}/{self.statements_run} statements | "
            f"{len(self.ddl_errors)} engine error(s) | "
            f"{len(self.error_issues)} enterprise error(s), {len(self.advisory_issues)} advisory | "
            f"live-schema score {self.enterprise_score}/100"
        )


class _NoBackend(RuntimeError):
    """Raised when neither a DSN nor a Docker/testcontainers backend is available."""


# ── backend acquisition ─────────────────────────────────────────────

def _drop_new_tables(pymysql, base: dict, db: str, keep: set[str]) -> None:
    """Drop every table in ``db`` that was not in ``keep`` — the cleanup path
    when we could not get an isolated scratch database."""
    try:
        conn = pymysql.connect(**base)
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                (db,),
            )
            for (name,) in list(cur.fetchall()):
                if name not in keep:
                    cur.execute(f"DROP TABLE IF EXISTS `{db}`.`{name}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        conn.close()
    except Exception as e:  # pragma: no cover
        logger.warning("execval: table cleanup in `%s` failed: %s", db, e)


@contextmanager
def _acquire_mysql() -> Iterator[tuple[dict, str, str]]:
    """Yield ``(connect_kwargs, schema_name, backend_name)`` for a throwaway
    MySQL database, cleaning it up on exit.

    ``connect_kwargs`` is ready to splat into ``pymysql.connect(**kwargs)``.
    """
    dsn = settings.MYSQL_EXEC_VALIDATION_DSN or os.environ.get("MYSQL_EXEC_VALIDATION_DSN")

    if dsn:
        import pymysql
        base = _parse_dsn(dsn)
        scratch = f"execval_{uuid.uuid4().hex[:8]}"
        admin = {k: v for k, v in base.items() if k != "database"}

        # Preferred path: the DSN user can CREATE DATABASE — full isolation,
        # dropped wholesale on exit.
        try:
            conn = pymysql.connect(**admin)
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE `{scratch}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()
            conn.close()
        except pymysql.err.MySQLError as e:
            # Fall back to the DSN's own database (managed servers often forbid
            # CREATE DATABASE). We only drop the tables *we* create.
            if not base.get("database"):
                raise _NoBackend(
                    f"DSN user cannot CREATE DATABASE ({e}) and the DSN names no "
                    f"database to fall back to — grant CREATE or add /<db> to the DSN"
                )
            db = base["database"]
            logger.info("execval: CREATE DATABASE denied (%s); using existing db `%s`", e, db)
            conn = pymysql.connect(**base)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                    (db,),
                )
                preexisting = {r[0] for r in cur.fetchall()}
            conn.close()
            try:
                yield ({**base}, db, "dsn")
            finally:
                _drop_new_tables(pymysql, base, db, preexisting)
            return

        try:
            yield ({**base, "database": scratch}, scratch, "dsn")
        finally:
            try:
                conn = pymysql.connect(**admin)
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS `{scratch}`")
                conn.commit()
                conn.close()
            except Exception as e:  # pragma: no cover
                logger.warning("execval: failed to drop scratch schema %s: %s", scratch, e)
        return

    if settings.MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER:
        MySqlContainer = None
        try:  # testcontainers >= 4.9 moved the module
            from testcontainers.community.mysql import MySqlContainer  # type: ignore
        except Exception:
            try:
                from testcontainers.mysql import MySqlContainer  # type: ignore
            except Exception as e:
                raise _NoBackend(
                    "no MYSQL_EXEC_VALIDATION_DSN set and testcontainers is not installed "
                    f"({e}); install `testcontainers[mysql]` or point the DSN at a MySQL 8 server"
                )
        try:
            container = MySqlContainer("mysql:8.0")
            container.start()
        except Exception as e:
            raise _NoBackend(
                f"could not start an ephemeral MySQL container (is Docker running?): {e}"
            )
        try:
            # get_connection_url() -> mysql+pymysql://test:test@host:port/test
            # — parse it so we don't depend on the .username/.dbname attribute
            # names, which have moved between testcontainers versions.
            kwargs = _parse_dsn(container.get_connection_url())
            schema = kwargs.get("database") or "test"
            yield (kwargs, schema, "testcontainers")
        finally:
            try:
                container.stop()
            except Exception as e:  # pragma: no cover
                logger.warning("execval: failed to stop MySQL container: %s", e)
        return

    raise _NoBackend(
        "MySQL execution validation is enabled but no backend is available: "
        "set MYSQL_EXEC_VALIDATION_DSN or allow MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER with Docker running"
    )


def _parse_dsn(dsn: str) -> dict:
    """Accept ``mysql://user:pass@host:port/db`` or an SQLAlchemy-style
    ``mysql+pymysql://…`` URL and return pymysql connect kwargs."""
    m = re.match(
        r"^(?:mysql(?:\+\w+)?)://(?P<user>[^:@/]+)(?::(?P<password>[^@/]*))?@"
        r"(?P<host>[^:/]+)(?::(?P<port>\d+))?(?:/(?P<database>[^?]*))?",
        dsn.strip(),
    )
    if not m:
        raise ValueError(f"unparseable MYSQL_EXEC_VALIDATION_DSN: {dsn!r}")
    g = m.groupdict()
    timeout = max(5, int(getattr(settings, "MYSQL_EXEC_VALIDATION_TIMEOUT", 90)))
    kwargs: dict = {
        "host": g["host"],
        "port": int(g["port"] or 3306),
        "user": g["user"],
        "password": g["password"] or "",
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": timeout,
        "read_timeout": timeout,
        "write_timeout": timeout,
    }
    if g.get("database"):
        kwargs["database"] = g["database"].split("?")[0]
    return kwargs


# ── DDL preparation ─────────────────────────────────────────────────

_SKIP_STMT_RE = re.compile(
    r"^\s*("
    r"START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK"
    # the stitched output does `SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO"`, which
    # *replaces* sql_mode and drops STRICT — swallow any sql_mode assignment so
    # the validator's own STRICT_ALL_TABLES stays authoritative.
    r"|SET\s+(?:SESSION\s+|GLOBAL\s+|@@)?(?:SESSION\.)?SQL_MODE\s*="
    r")",
    re.IGNORECASE,
)
_COMMENT_LINE_RE = re.compile(r"^\s*(--|#).*$", re.MULTILINE)


def _split_statements(ddl: str) -> list[str]:
    """Split a script into individual statements. Prefers ``sqlparse`` (handles
    ``;`` inside strings / comments); falls back to a naive split.

    Drops transaction-control and ``SET sql_mode`` statements (see
    ``_SKIP_STMT_RE``); everything else — including ``SET FOREIGN_KEY_CHECKS`` —
    is executed as written."""
    try:
        import sqlparse
        parts = sqlparse.split(ddl)
    except Exception:  # pragma: no cover
        parts = ddl.split(";")
    out = []
    for p in parts:
        s = _COMMENT_LINE_RE.sub("", p).strip().rstrip(";").strip()
        if not s or _SKIP_STMT_RE.match(s):
            continue
        out.append(s)
    return out


def _stmt_preview(stmt: str, n: int = 90) -> str:
    one = re.sub(r"\s+", " ", stmt).strip()
    return one[:n] + ("…" if len(one) > n else "")


# ── main entry point ────────────────────────────────────────────────

def execute_and_validate(ddl: str) -> ExecutionResult:
    """Run ``ddl`` against a real MySQL 8 and return a structured
    :class:`ExecutionResult`. Never raises for a missing backend — that comes
    back as ``skipped=True``."""
    t0 = time.time()
    import pymysql
    from pymysql.err import MySQLError

    result = ExecutionResult(success=False, executed=False)
    statements = _split_statements(ddl)

    try:
        backend_cm = _acquire_mysql()
        _entered = backend_cm.__enter__()
    except _NoBackend as e:
        return ExecutionResult(
            success=False, executed=False, skipped=True, skip_reason=str(e),
            duration_seconds=round(time.time() - t0, 2),
        )
    except Exception as e:  # misconfigured DSN, unreachable host, pull blocked …
        logger.error("[execval] backend unavailable: %s", e, exc_info=True)
        return ExecutionResult(
            success=False, executed=False, skipped=True,
            skip_reason=f"backend error: {e}",
            duration_seconds=round(time.time() - t0, 2),
        )

    try:
        connect_kwargs, schema, backend = _entered
        result.backend = backend
        conn = pymysql.connect(**connect_kwargs)
        try:
            result.executed = True
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                result.mysql_version = cur.fetchone()[0]
                # STRICT_ALL_TABLES so invalid defaults / bad values are hard
                # errors on every engine, not silent coercions.
                cur.execute(
                    "SET SESSION sql_mode = "
                    "'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,ERROR_FOR_DIVISION_BY_ZERO'"
                )

            for stmt in statements:
                result.statements_run += 1
                try:
                    with conn.cursor() as cur:
                        cur.execute(stmt)
                        # drain any statement that returned rows
                        try:
                            cur.fetchall()
                        except Exception:
                            pass
                    conn.commit()
                except MySQLError as e:  # noqa: PERF203 - we want per-statement capture
                    result.statements_failed += 1
                    errno = e.args[0] if e.args else None
                    msg = e.args[1] if len(e.args) > 1 else str(e)
                    # verbatim — errno + MySQL's own wording, plus which stmt
                    result.ddl_errors.append(
                        f"[MySQL {errno}] {msg}  (at: {_stmt_preview(stmt)})"
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            # Enterprise checks run on whatever successfully loaded.
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    result.tables_created = _list_base_tables(cur, schema)
                    if result.tables_created:
                        result.issues.extend(_enterprise_checks(cur, schema))
            except Exception as e:  # pragma: no cover - introspection shouldn't fail
                result.warnings.append(f"enterprise checks incomplete: {e}")
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover - unexpected driver / network fault
        result.warnings.append(f"execution aborted: {e}")
        logger.error("[execval] aborted: %s", e, exc_info=True)
    finally:
        try:
            backend_cm.__exit__(None, None, None)
        except Exception as e:  # pragma: no cover
            logger.warning("[execval] backend teardown failed: %s", e)

    result.success = result.executed and not result.ddl_errors
    result.enterprise_score = max(
        0,
        100
        - sum(_SCORE_PENALTY[i.severity] for i in result.issues)
        - (10 if result.ddl_errors else 0),
    )
    result.duration_seconds = round(time.time() - t0, 2)
    logger.info("[execval] %s", result.summary())
    return result


# ── introspection helpers ──────────────────────────────────────────

def _list_base_tables(cur, schema: str) -> list[str]:
    cur.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME",
        (schema,),
    )
    return [r["TABLE_NAME"] for r in cur.fetchall()]


def _columns_by_table(cur, schema: str) -> dict[str, list[dict]]:
    cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        (schema,),
    )
    out: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        out.setdefault(r["TABLE_NAME"], []).append(r)
    return out


def _fks(cur, schema: str) -> list[dict]:
    cur.execute(
        """
        SELECT rc.CONSTRAINT_NAME     AS name,
               rc.TABLE_NAME          AS child_table,
               kcu.COLUMN_NAME        AS child_col,
               rc.REFERENCED_TABLE_NAME AS parent_table,
               kcu.REFERENCED_COLUMN_NAME AS parent_col,
               rc.DELETE_RULE         AS delete_rule,
               rc.UPDATE_RULE         AS update_rule
        FROM information_schema.REFERENTIAL_CONSTRAINTS rc
        JOIN information_schema.KEY_COLUMN_USAGE kcu
          ON kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
         AND kcu.CONSTRAINT_NAME   = rc.CONSTRAINT_NAME
         AND kcu.TABLE_NAME        = rc.TABLE_NAME
        WHERE rc.CONSTRAINT_SCHEMA=%s
        """,
        (schema,),
    )
    return list(cur.fetchall())


def _secondary_indexes(cur, schema: str) -> dict[str, list[dict]]:
    """Non-PRIMARY indexes per table, as ordered column lists."""
    cur.execute(
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
        "AND INDEX_NAME <> 'PRIMARY' ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
        (schema,),
    )
    grouped: dict[tuple[str, str], dict] = {}
    for r in cur.fetchall():
        key = (r["TABLE_NAME"], r["INDEX_NAME"])
        g = grouped.setdefault(key, {"table": r["TABLE_NAME"], "name": r["INDEX_NAME"],
                                     "non_unique": r["NON_UNIQUE"], "cols": []})
        g["cols"].append(r["COLUMN_NAME"])
    by_table: dict[str, list[dict]] = {}
    for g in grouped.values():
        by_table.setdefault(g["table"], []).append(g)
    return by_table


def _leading_index_cols(cur, schema: str) -> dict[str, set[str]]:
    """First column of *every* index (PRIMARY included) per table — a FK column
    that leads any index (or the PK) is considered index-covered for joins."""
    cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND SEQ_IN_INDEX=1",
        (schema,),
    )
    out: dict[str, set[str]] = {}
    for r in cur.fetchall():
        out.setdefault(r["TABLE_NAME"], set()).add(r["COLUMN_NAME"])
    return out


def _table_meta(cur, schema: str) -> dict[str, dict]:
    cur.execute(
        "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
        (schema,),
    )
    return {r["TABLE_NAME"]: r for r in cur.fetchall()}


def _is_pure_junction(cols: list[dict], fk_child_cols: set[str]) -> bool:
    """Live-schema mirror of the miner's ``is_probable_junction`` heuristic:
    >=2 ``*_id`` columns, <=4 non-timestamp columns, and every non-timestamp
    column is either an ``*_id`` / formal-FK column or id/status/sort scaffolding.

    ``*_id`` columns are treated as FK-intent (this generator emits comment-FKs
    under SET FOREIGN_KEY_CHECKS = 0), so a formal CONSTRAINT is not required."""
    names = [c["COLUMN_NAME"].lower() for c in cols]
    id_like = {n for n in names if n.endswith("_id") and n != "id"}
    link_cols = id_like | {c.lower() for c in fk_child_cols}
    if len(link_cols) < 2:
        return False
    non_audit = [n for n in names if n not in _CREATED_COLS + _UPDATED_COLS]
    if len(non_audit) > 4:
        return False
    allowed = link_cols | {"id", "status", "sort_order", "position",
                           "is_active", "sequence"}
    return all(n in allowed for n in non_audit)


# ── enterprise-grade checks ────────────────────────────────────────
# Every threshold below is quoted from app/validators/reference_thresholds.json,
# mined from the ~34 real production dumps in Databases/. See
# scripts/reference_findings.md for the full write-up.

def _enterprise_checks(cur, schema: str) -> list[ExecutionIssue]:
    issues: list[ExecutionIssue] = []
    cols_by_table = _columns_by_table(cur, schema)
    fks = _fks(cur, schema)
    idx_by_table = _secondary_indexes(cur, schema)
    meta = _table_meta(cur, schema)

    issues += _check_fk_referential_actions(fks)
    issues += _check_fk_type_alignment(fks, cols_by_table)
    issues += _check_multi_fk_secondary_index(cols_by_table, _leading_index_cols(cur, schema))
    issues += _check_missing_timestamps(cols_by_table, fks)
    issues += _check_engine(meta)
    issues += _check_charset_consistency(meta)
    issues += _check_redundant_indexes(idx_by_table)
    return issues


def _check_fk_referential_actions(fks: list[dict]) -> list[ExecutionIssue]:
    """Empirical basis: of 127 FK constraints in the real dumps, only 48% spell
    out ON DELETE and 15% ON UPDATE — the rest inherit MySQL's silent RESTRICT.
    (reference_thresholds.json → foreign_keys). Advisory, not error: an implicit
    RESTRICT is legal, just rarely intentional. When a real dump *is* explicit it
    picks CASCADE 60/61 times, so that is the suggested default."""
    rate = _TH.get("foreign_keys", {}).get("with_explicit_on_delete_pct", 48.0)
    suggest = _DT.get("most_common_explicit_on_delete", "CASCADE")
    out = []
    for fk in fks:
        d, u = (fk["delete_rule"] or "").upper(), (fk["update_rule"] or "").upper()
        if d in ("RESTRICT", "NO ACTION") and u in ("RESTRICT", "NO ACTION"):
            out.append(ExecutionIssue(
                severity="advisory",
                category="fk-no-referential-action",
                message=(
                    f"FK `{fk['name']}` on {fk['child_table']}.{fk['child_col']} → "
                    f"{fk['parent_table']}.{fk['parent_col']} leaves ON DELETE / ON UPDATE "
                    f"at the implicit RESTRICT. Only {rate}% of real-dump FKs are left "
                    f"this way deliberately — state the intent explicitly "
                    f"(e.g. ON DELETE {suggest} / SET NULL where the child is optional)."
                ),
                table=fk["child_table"],
                object_name=fk["name"],
            ))
    return out


def _check_fk_type_alignment(fks: list[dict], cols_by_table: dict) -> list[ExecutionIssue]:
    """A FK whose column type differs from the referenced column's type. MySQL
    normally rejects this at CREATE (errno 3780/1215) — but a script that runs
    with SET FOREIGN_KEY_CHECKS = 0 (as the stitched output does) can load it
    anyway, so we re-check structurally. Error severity."""
    out = []
    type_of = {
        (t, c["COLUMN_NAME"].lower()): c["COLUMN_TYPE"].lower()
        for t, cs in cols_by_table.items() for c in cs
    }
    for fk in fks:
        ct = type_of.get((fk["child_table"], (fk["child_col"] or "").lower()))
        pt = type_of.get((fk["parent_table"], (fk["parent_col"] or "").lower()))
        if ct and pt and _normalise_type(ct) != _normalise_type(pt):
            out.append(ExecutionIssue(
                severity="error",
                category="fk-type-mismatch",
                message=(
                    f"FK `{fk['name']}`: {fk['child_table']}.{fk['child_col']} is {ct} but "
                    f"{fk['parent_table']}.{fk['parent_col']} is {pt}. MySQL requires "
                    f"compatible types (same signedness, width, charset) for a foreign key."
                ),
                table=fk["child_table"],
                object_name=fk["name"],
            ))
    return out


def _normalise_type(t: str) -> str:
    # display width on integers is cosmetic in MySQL 8; unsigned / charset are not
    t = re.sub(r"\bint\(\d+\)", "int", t)
    return re.sub(r"\s+", " ", t).strip()


_ID_COL_RE = re.compile(r".+_id$")


def _check_multi_fk_secondary_index(cols_by_table: dict, leading_cols: dict) -> list[ExecutionIssue]:
    """Empirical basis: every real-dump table carrying more than one FK also
    leads an index with each FK column (33/33 = 100%,
    reference_thresholds.json → indexes). An unindexed FK column table-scans on
    joins and on parent deletes. Advisory.

    This generator emits its schema with ``SET FOREIGN_KEY_CHECKS = 0`` and
    models most relationships as ``*_id`` columns with a COMMENT rather than a
    declared CONSTRAINT (that is how the reference dumps are built too — only
    9/34 declare any FK at all). So the check treats every ``<name>_id`` column
    as FK-intent, not just formal constraints, and a column counts as covered if
    it leads any index, PRIMARY included."""
    out = []
    for table, cols in cols_by_table.items():
        id_cols = [c["COLUMN_NAME"] for c in cols
                   if _ID_COL_RE.match(c["COLUMN_NAME"].lower()) and c["COLUMN_NAME"].lower() != "id"]
        if len(id_cols) <= 1:
            continue
        covered = leading_cols.get(table, set())
        uncovered = sorted(c for c in id_cols if c not in covered)
        if uncovered:
            out.append(ExecutionIssue(
                severity="advisory",
                category="multi-fk-missing-index",
                message=(
                    f"{table} has {len(id_cols)} foreign-key columns but no leading index on "
                    f"{', '.join(uncovered)}. In the reference dumps 100% of multi-FK "
                    f"tables index every FK column — add KEY idx_{table}_<col> (<col>)."
                ),
                table=table,
            ))
    return out


def _check_missing_timestamps(cols_by_table: dict, fks: list[dict]) -> list[ExecutionIssue]:
    """Empirical basis: only ~33% of real base tables carry any audit timestamp
    (reference_thresholds.json → timestamps) — so this is an advisory, not an
    error. Pure junction tables are exempt (architectural call; the strict
    junction heuristic matched too few tables in the corpus to calibrate on).
    Accepts both the dominant `created_on`/`modified_on` and the `_at` variants."""
    fk_child_cols: dict[str, set[str]] = {}
    for fk in fks:
        fk_child_cols.setdefault(fk["child_table"], set()).add((fk["child_col"] or "").lower())
    out = []
    for table, cols in cols_by_table.items():
        names = {c["COLUMN_NAME"].lower() for c in cols}
        if _is_pure_junction(cols, fk_child_cols.get(table, set())):
            continue
        has_created = any(c in names for c in _CREATED_COLS)
        has_updated = any(c in names for c in _UPDATED_COLS)
        if not has_created and not has_updated:
            out.append(ExecutionIssue(
                severity="advisory",
                category="missing-timestamps",
                message=(
                    f"{table} has neither a created (`created_on`/`created_at`) nor an "
                    f"updated (`modified_on`/`updated_at`) timestamp. Not a junction "
                    f"table — add both for auditability."
                ),
                table=table,
            ))
    return out


def _check_engine(meta: dict) -> list[ExecutionIssue]:
    """Empirical basis: 96.9% of real-dump tables are InnoDB
    (reference_thresholds.json → engine). MyISAM has no transactions, row
    locking, FK support or crash recovery — the proprietary patterns assume
    InnoDB. Error severity."""
    out = []
    for table, m in meta.items():
        eng = (m.get("ENGINE") or "").lower()
        if eng and eng != "innodb":
            out.append(ExecutionIssue(
                severity="error",
                category="non-innodb",
                message=(
                    f"{table} uses ENGINE={m['ENGINE']} — must be InnoDB "
                    f"(transactions, row locking, FK support, crash recovery)."
                ),
                table=table,
            ))
    return out


def _check_charset_consistency(meta: dict) -> list[ExecutionIssue]:
    """Empirical basis: 79.4% of real dumps mix >1 charset across their tables
    (a latin1 legacy core with utf8mb4 bolted on); utf8mb4 is only 34.6% of
    tables (reference_thresholds.json → charset). This check is what a *new*
    schema should do better: one charset, and utf8mb4. Advisory."""
    charsets = {}
    for table, m in meta.items():
        coll = (m.get("TABLE_COLLATION") or "")
        cs = coll.split("_", 1)[0].lower() if coll else ""
        if cs:
            charsets.setdefault(cs, []).append(table)
    out = []
    legacy = {cs: t for cs, t in charsets.items() if cs in _LEGACY_CHARSETS}
    if legacy:
        for cs, tabs in legacy.items():
            out.append(ExecutionIssue(
                severity="advisory",
                category="legacy-charset",
                message=(
                    f"{len(tabs)} table(s) use CHARSET {cs} "
                    f"({', '.join(sorted(tabs)[:5])}{'…' if len(tabs) > 5 else ''}) — "
                    f"latin1/utf8mb3 cannot store Devanagari or emoji. Use utf8mb4."
                ),
                object_name=cs,
            ))
    non_legacy = [cs for cs in charsets if cs not in _LEGACY_CHARSETS]
    if len(charsets) > 1 and not (len(legacy) == 0 and len(non_legacy) == 1):
        out.append(ExecutionIssue(
            severity="advisory",
            category="charset-inconsistency",
            message=(
                f"Schema mixes {len(charsets)} charsets ({', '.join(sorted(charsets))}). "
                f"Cross-charset joins on string keys force a slow conversion — "
                f"standardise on utf8mb4 / utf8mb4_unicode_ci everywhere."
            ),
        ))
    return out


def _check_redundant_indexes(idx_by_table: dict) -> list[ExecutionIssue]:
    """A secondary index whose column list is a prefix of (or identical to)
    another index on the same table is dead weight on every write. Advisory."""
    out = []
    for table, groups in idx_by_table.items():
        seqs = [(g["name"], tuple(g["cols"])) for g in groups]
        for i, (name_a, cols_a) in enumerate(seqs):
            for name_b, cols_b in seqs[i + 1:]:
                if cols_a == cols_b or (
                    len(cols_a) < len(cols_b) and cols_b[:len(cols_a)] == cols_a
                ) or (
                    len(cols_b) < len(cols_a) and cols_a[:len(cols_b)] == cols_b
                ):
                    shorter, longer = sorted([name_a, name_b], key=lambda n: len(
                        dict(seqs)[n]))
                    out.append(ExecutionIssue(
                        severity="advisory",
                        category="redundant-index",
                        message=(
                            f"{table}: index `{shorter}` is redundant — it is a prefix of "
                            f"`{longer}`. Drop it to speed up writes."
                        ),
                        table=table,
                        object_name=shorter,
                    ))
    return out
