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

The enterprise checks below are grounded in documented, cited best practice —
official MySQL/PostgreSQL documentation, respected database-design literature,
and real company engineering writeups — not in observed-average compliance
from the 34 real production dumps in ``Databases/``. Those dumps remain
useful as domain-narrative reference material (what a financial-ledger or
logistics schema's entities look like) but are no longer the quality bar; see
``docs/enterprise_standards_spec.md`` for the research and rationale behind
each check below. (The original empirical mining — ``scripts/mine_reference_dumps.py``,
``app/validators/reference_thresholds.json``, ``scripts/reference_findings.md``
— is kept as a historical record of how the checks were first discovered, not
as a live source of truth.)
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Iterator, Literal, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

Severity = Literal["error", "advisory"]

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


# ── multi-schema validation (decomposed projects) ──────────────────
# docs/enterprise_standards_spec.md §2.4/§6. Additive: execute_and_validate
# above is completely untouched, so the default single-schema path is
# unaffected by this section's existence.

@contextmanager
def _acquire_mysql_multi(schema_names: list[str]) -> Iterator[tuple[dict, dict[str, str], str]]:
    """Like ``_acquire_mysql``, but creates one throwaway database PER schema
    name, all on the same server connection — so a foreign key between two
    of them is something MySQL can actually create (same-server cross-
    database FKs work in MySQL/InnoDB) and therefore something
    ``_check_cross_schema_fk_violations`` can actually detect once every
    schema is loaded together. Yields
    ``(admin_connect_kwargs, {schema_name: real_db_name}, backend_name)``.
    Deliberately independent of ``_acquire_mysql`` (some duplication
    accepted) rather than refactoring it, so the existing single-schema path
    can't be affected by this addition."""
    dsn = settings.MYSQL_EXEC_VALIDATION_DSN or os.environ.get("MYSQL_EXEC_VALIDATION_DSN")

    def _real_db_names() -> dict[str, str]:
        return {
            s: f"execval_{re.sub(r'[^a-z0-9_]', '_', s.lower())}_{uuid.uuid4().hex[:6]}"
            for s in schema_names
        }

    if dsn:
        import pymysql
        base = _parse_dsn(dsn)
        admin = {k: v for k, v in base.items() if k != "database"}
        real_db = _real_db_names()
        try:
            conn = pymysql.connect(**admin)
            with conn.cursor() as cur:
                for db in real_db.values():
                    cur.execute(
                        f"CREATE DATABASE `{db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
            conn.commit()
            conn.close()
        except Exception as e:
            raise _NoBackend(
                f"DSN user cannot CREATE DATABASE for multi-schema validation ({e}) — "
                f"multi-schema validation needs several independent databases at once, "
                f"so the existing-database fallback single-schema validation uses isn't "
                f"available here; grant CREATE"
            )
        try:
            yield admin, real_db, "dsn"
        finally:
            try:
                conn = pymysql.connect(**admin)
                with conn.cursor() as cur:
                    # A cross-schema FK slipping through (exactly what this
                    # module exists to detect) would otherwise make the
                    # parent database undroppable while the child's FK is live.
                    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
                    for db in real_db.values():
                        cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
                    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
                conn.commit()
                conn.close()
            except Exception as e:  # pragma: no cover
                logger.warning("execval: failed to drop multi-schema scratch DBs: %s", e)
        return

    if settings.MYSQL_EXEC_VALIDATION_USE_TESTCONTAINER:
        try:
            try:
                from testcontainers.community.mysql import MySqlContainer  # type: ignore
            except Exception:
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
            import pymysql
            admin = _parse_dsn(container.get_connection_url())
            admin.pop("database", None)
            real_db = _real_db_names()
            conn = pymysql.connect(**admin)
            with conn.cursor() as cur:
                for db in real_db.values():
                    cur.execute(
                        f"CREATE DATABASE `{db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
            conn.commit()
            conn.close()
            yield admin, real_db, "testcontainers"
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


def execute_and_validate_schemas(schemas: dict[str, str]) -> dict[str, ExecutionResult]:
    """Multi-schema counterpart to ``execute_and_validate`` — runs each
    schema's DDL into its own real database on the SAME MySQL server, then
    checks for any foreign key crossing a schema boundary across the full
    set together (docs/enterprise_standards_spec.md §2.4/§6). Each schema's
    own ``ExecutionResult`` is otherwise exactly what ``execute_and_validate``
    would have produced for it standalone — the cross-schema check is the
    only thing that genuinely can't be computed one schema at a time."""
    t0 = time.time()
    import pymysql
    from pymysql.err import MySQLError

    schema_names = list(schemas)
    results = {name: ExecutionResult(success=False, executed=False) for name in schema_names}

    try:
        backend_cm = _acquire_mysql_multi(schema_names)
        admin_kwargs, real_db, backend = backend_cm.__enter__()
    except _NoBackend as e:
        for r in results.values():
            r.skipped, r.skip_reason = True, str(e)
            r.duration_seconds = round(time.time() - t0, 2)
        return results
    except Exception as e:  # pragma: no cover - misconfigured DSN, unreachable host …
        logger.error("[execval] multi-schema backend unavailable: %s", e, exc_info=True)
        for r in results.values():
            r.skipped, r.skip_reason = True, f"backend error: {e}"
            r.duration_seconds = round(time.time() - t0, 2)
        return results

    try:
        for name in schema_names:
            r = results[name]
            r.backend = backend
            conn = pymysql.connect(**{**admin_kwargs, "database": real_db[name]})
            try:
                r.executed = True
                with conn.cursor() as cur:
                    cur.execute("SELECT VERSION()")
                    r.mysql_version = cur.fetchone()[0]
                    cur.execute(
                        "SET SESSION sql_mode = "
                        "'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,ERROR_FOR_DIVISION_BY_ZERO'"
                    )
                for stmt in _split_statements(schemas[name]):
                    r.statements_run += 1
                    try:
                        with conn.cursor() as cur:
                            cur.execute(stmt)
                            try:
                                cur.fetchall()
                            except Exception:
                                pass
                        conn.commit()
                    except MySQLError as e:  # noqa: PERF203
                        r.statements_failed += 1
                        errno = e.args[0] if e.args else None
                        msg = e.args[1] if len(e.args) > 1 else str(e)
                        r.ddl_errors.append(f"[MySQL {errno}] {msg}  (at: {_stmt_preview(stmt)})")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
            finally:
                conn.close()

        # Per-schema enterprise checks, once every schema has finished loading.
        for name in schema_names:
            r = results[name]
            try:
                conn = pymysql.connect(**{**admin_kwargs, "database": real_db[name]})
                try:
                    with conn.cursor(pymysql.cursors.DictCursor) as cur:
                        r.tables_created = _list_base_tables(cur, real_db[name])
                        if r.tables_created:
                            r.issues.extend(_enterprise_checks(cur, real_db[name]))
                finally:
                    conn.close()
            except Exception as e:  # pragma: no cover
                r.warnings.append(f"enterprise checks incomplete: {e}")

        # Cross-schema FK check — the one thing that needs the full set at once.
        try:
            conn = pymysql.connect(**admin_kwargs)
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    violations = _check_cross_schema_fk_violations(cur, list(real_db.values()))
            finally:
                conn.close()
            db_to_schema = {db: name for name, db in real_db.items()}
            for v in violations:
                owner = db_to_schema.get((v.table or "").split(".")[0])
                if owner in results:
                    results[owner].issues.append(v)
        except Exception as e:  # pragma: no cover
            for r in results.values():
                r.warnings.append(f"cross-schema FK check incomplete: {e}")

    except Exception as e:  # pragma: no cover - unexpected driver / network fault
        for r in results.values():
            r.warnings.append(f"execution aborted: {e}")
        logger.error("[execval] multi-schema execution aborted: %s", e, exc_info=True)
    finally:
        try:
            backend_cm.__exit__(None, None, None)
        except Exception as e:  # pragma: no cover
            logger.warning("[execval] multi-schema backend teardown failed: %s", e)

    for name in schema_names:
        r = results[name]
        r.success = r.executed and not r.ddl_errors
        r.enterprise_score = max(
            0, 100 - sum(_SCORE_PENALTY[i.severity] for i in r.issues) - (10 if r.ddl_errors else 0),
        )
        r.duration_seconds = round(time.time() - t0, 2)
        logger.info("[execval:%s] %s", name, r.summary())
    return results


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
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, IS_NULLABLE "
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

    issues += _check_fk_referential_actions(fks, cols_by_table)
    issues += _check_soft_delete_cascade_conflict(fks, cols_by_table)
    issues += _check_fk_type_alignment(fks, cols_by_table)
    issues += _check_multi_fk_secondary_index(cols_by_table, _leading_index_cols(cur, schema))
    issues += _check_missing_timestamps(cols_by_table, fks)
    issues += _check_engine(meta)
    issues += _check_charset_consistency(meta)
    issues += _check_redundant_indexes(idx_by_table)
    return issues


# Enterprise-grade referential-action guidance (see docs/enterprise_standards_spec.md
# §1.1 / §2.1). Not calibrated from Databases/ — there is no authoritative source
# for "X% of FKs should be explicit"; what's well-documented is a real decision
# framework (PostgreSQL's own docs) for WHICH action fits a relationship, plus a
# hard MySQL constraint (InnoDB rejects SET DEFAULT outright) and a documented
# incompatibility (CASCADE never fires on the UPDATE a soft-delete performs, so
# it must never be suggested for a soft-deletable parent — see
# _check_soft_delete_cascade_conflict below for the "already wrong" case).
_SOFT_DELETE_COLS = {"deleted_at", "deleted_on", "is_deleted"}

# Suffixes this generator's own naming convention uses for a table that is a
# structural *component* of its "_header_all" parent and cannot meaningfully
# exist without it (line items, the archive mirror, the lifecycle trail) —
# PostgreSQL's docs: "when the referencing table represents something that is
# a component of what is represented by the referenced table and cannot exist
# independently, then CASCADE could be appropriate."
_OWNED_CHILD_SUFFIXES = ("_details_all", "_detail_all", "_line_item_all",
                         "_life_cycle_all", "_archive_all")
_TABLE_BASE_SUFFIXES = (
    "_header_all", "_transaction_all", "_configuration_all",
    "_details_all", "_detail_all", "_line_item_all",
    "_life_cycle_all", "_archive_all", "_all",
)


def _table_base(name: str) -> str:
    """Strip this generator's own role suffix to get the entity's base name,
    e.g. 'invoice_details_all' and 'invoice_header_all' both -> 'invoice'."""
    lname = name.lower()
    for suf in _TABLE_BASE_SUFFIXES:
        if lname.endswith(suf):
            return lname[: -len(suf)]
    return lname


def _has_soft_delete_column(cols: list[dict]) -> Optional[str]:
    for c in cols:
        if c["COLUMN_NAME"].lower() in _SOFT_DELETE_COLS:
            return c["COLUMN_NAME"]
    return None


def _is_owned_child(child_table: str, parent_table: str) -> bool:
    return (child_table.lower().endswith(_OWNED_CHILD_SUFFIXES)
            and _table_base(child_table) == _table_base(parent_table))


def _fk_column_nullable(fk: dict, cols_by_table: dict) -> bool:
    for c in cols_by_table.get(fk["child_table"], []):
        if c["COLUMN_NAME"].lower() == (fk["child_col"] or "").lower():
            return (c.get("IS_NULLABLE") or "").upper() == "YES"
    return False


def _suggest_fk_actions(fk: dict, cols_by_table: dict) -> tuple[str, str, str]:
    """Returns (on_delete, on_update, reason) for an FK currently left at the
    implicit RESTRICT/NO ACTION on both sides. Relationship-aware, per
    PostgreSQL's documented decision framework — not a single blanket
    suggestion. Never returns SET DEFAULT: InnoDB rejects it outright at
    CREATE TABLE time, so it is never a usable suggestion on MySQL."""
    parent_cols = cols_by_table.get(fk["parent_table"], [])
    soft_delete_col = _has_soft_delete_column(parent_cols)
    if soft_delete_col:
        return ("RESTRICT", "CASCADE", (
            f"{fk['parent_table']} is soft-deletable (has a `{soft_delete_col}` "
            f"column) — a soft delete is an UPDATE, so ON DELETE CASCADE would "
            f"never actually fire and gives a false sense of automatic cleanup. "
            f"Keep ON DELETE RESTRICT and clean up children explicitly in code."
        ))
    if _is_owned_child(fk["child_table"], fk["parent_table"]):
        return ("CASCADE", "CASCADE", (
            f"{fk['child_table']} is a structural component of "
            f"{fk['parent_table']} (line items / archive / lifecycle trail) and "
            f"cannot meaningfully exist without it — CASCADE is the documented "
            f"fit for an owned-child relationship."
        ))
    if _fk_column_nullable(fk, cols_by_table):
        return ("SET NULL", "CASCADE", (
            f"{fk['child_table']}.{fk['child_col']} is nullable — this FK "
            f"represents an optional reference, so SET NULL fits better than "
            f"blocking the delete."
        ))
    return ("RESTRICT", "CASCADE", (
        f"{fk['child_table']} and {fk['parent_table']} are independent "
        f"entities (neither owns the other) — RESTRICT is the documented fit "
        f"so an accidental delete of a referenced row is blocked, not silently "
        f"propagated."
    ))


def _check_fk_referential_actions(fks: list[dict], cols_by_table: dict) -> list[ExecutionIssue]:
    """Every FK left at the implicit RESTRICT/NO ACTION on both sides gets a
    relationship-aware suggestion (see _suggest_fk_actions) instead of one
    blanket "use CASCADE" nudge. Advisory: an implicit RESTRICT is legal SQL,
    just rarely an intentional choice — the goal is to make the author pick
    consciously, not to force one specific action."""
    out = []
    for fk in fks:
        d, u = (fk["delete_rule"] or "").upper(), (fk["update_rule"] or "").upper()
        if d in ("RESTRICT", "NO ACTION") and u in ("RESTRICT", "NO ACTION"):
            on_delete, on_update, reason = _suggest_fk_actions(fk, cols_by_table)
            out.append(ExecutionIssue(
                severity="advisory",
                category="fk-no-referential-action",
                message=(
                    f"FK `{fk['name']}` on {fk['child_table']}.{fk['child_col']} → "
                    f"{fk['parent_table']}.{fk['parent_col']} leaves ON DELETE / ON UPDATE "
                    f"at the implicit RESTRICT — state the intent explicitly: "
                    f"ON DELETE {on_delete} ON UPDATE {on_update}. {reason}"
                ),
                table=fk["child_table"],
                object_name=fk["name"],
            ))
    return out


def _check_soft_delete_cascade_conflict(fks: list[dict], cols_by_table: dict) -> list[ExecutionIssue]:
    """A table with a soft-delete indicator column (deleted_at / deleted_on /
    is_deleted) that is ALSO the parent of an explicit ON DELETE CASCADE FK is
    a distinct, more specific finding than "action left implicit" above: here
    the action IS explicit, and it's actively the wrong one. A soft delete is
    an UPDATE, not a DELETE, so the CASCADE never fires — children of a
    "deleted" parent silently stay live and referencing it, which reads as
    safe (there's a CASCADE!) but isn't. Advisory, not error: this is a
    column-name heuristic (deleted_at/is_deleted could in principle be
    repurposed for something else), so it's a strong design nudge rather than
    a certain DDL-level defect the way a type mismatch or non-InnoDB engine
    is."""
    out = []
    for fk in fks:
        if (fk["delete_rule"] or "").upper() != "CASCADE":
            continue
        parent_cols = cols_by_table.get(fk["parent_table"], [])
        soft_delete_col = _has_soft_delete_column(parent_cols)
        if soft_delete_col:
            out.append(ExecutionIssue(
                severity="advisory",
                category="soft-delete-cascade-conflict",
                message=(
                    f"FK `{fk['name']}` on {fk['child_table']}.{fk['child_col']} → "
                    f"{fk['parent_table']}.{fk['parent_col']} is ON DELETE CASCADE, "
                    f"but {fk['parent_table']} is soft-deletable (has a "
                    f"`{soft_delete_col}` column). A soft delete is an UPDATE, not "
                    f"a DELETE, so this CASCADE will never fire — it looks like "
                    f"automatic cleanup but isn't. Use ON DELETE RESTRICT and "
                    f"handle child cleanup explicitly (or hard-delete instead)."
                ),
                table=fk["parent_table"],
                object_name=fk["name"],
            ))
    return out


def _partitioned_tables(cur, schema: str) -> set[str]:
    """Tables with at least one named partition. Not called from
    _enterprise_checks yet — nothing in the generator recommends partitioning
    today — but kept ready so _check_partition_fk_conflict can be wired in
    with a single call-site change the moment that guidance is added."""
    cur.execute(
        "SELECT DISTINCT TABLE_NAME FROM information_schema.PARTITIONS "
        "WHERE TABLE_SCHEMA=%s AND PARTITION_NAME IS NOT NULL",
        (schema,),
    )
    return {r["TABLE_NAME"] for r in cur.fetchall()}


def _check_partition_fk_conflict(partitioned_tables: set[str], fks: list[dict]) -> list[ExecutionIssue]:
    """Hard MySQL/InnoDB restriction, not a style preference: "InnoDB tables
    which have or which are referenced by foreign keys cannot be partitioned"
    — a partitioned table can neither declare an FK nor be an FK's target.
    Error severity: this isn't a judgment call, it's a DDL-level rejection
    waiting to happen the moment partitioning is actually applied. Not yet
    wired into _enterprise_checks (see _partitioned_tables) since nothing in
    the generator emits PARTITION BY today; exists so the check is ready the
    moment partitioning guidance lands."""
    out = []
    for fk in fks:
        if fk["child_table"] in partitioned_tables:
            out.append(ExecutionIssue(
                severity="error",
                category="partition-fk-conflict",
                message=(
                    f"{fk['child_table']} is partitioned and cannot declare "
                    f"foreign key `{fk['name']}` — InnoDB does not allow a "
                    f"partitioned table to have FK constraints."
                ),
                table=fk["child_table"], object_name=fk["name"],
            ))
        if fk["parent_table"] in partitioned_tables:
            out.append(ExecutionIssue(
                severity="error",
                category="partition-fk-conflict",
                message=(
                    f"{fk['parent_table']} is partitioned and cannot be "
                    f"referenced by foreign key `{fk['name']}` (from "
                    f"{fk['child_table']}) — InnoDB does not allow a "
                    f"partitioned table to be an FK target."
                ),
                table=fk["parent_table"], object_name=fk["name"],
            ))
    return out


def _check_cross_schema_fk_violations(cur, schema_names: list[str]) -> list[ExecutionIssue]:
    """Hard rule, not a suggestion (docs/enterprise_standards_spec.md
    §2.3/§5c): once a project is decomposed into multiple schemas, NO
    foreign key may cross a schema boundary — even though MySQL, on the
    same server, would happily create and enforce one.
    ``schema_decomposition.split_ddl_by_schema`` downgrades every FK it
    finds crossing a boundary at generation time; this is the
    defense-in-depth check that catches anything that slipped through (e.g.
    a standalone ``ALTER TABLE … ADD CONSTRAINT`` the splitter's per-table
    block scan doesn't see) once every one of a decomposed project's
    schemas is loaded together — see ``execute_and_validate_schemas``.
    Error severity: this is a design-boundary violation, not a performance
    nudge."""
    if len(schema_names) < 2:
        return []
    placeholders = ",".join(["%s"] * len(schema_names))
    cur.execute(
        f"""
        SELECT kcu.CONSTRAINT_SCHEMA AS child_schema, kcu.TABLE_NAME AS child_table,
               kcu.COLUMN_NAME AS child_col, kcu.CONSTRAINT_NAME AS name,
               kcu.REFERENCED_TABLE_SCHEMA AS parent_schema,
               kcu.REFERENCED_TABLE_NAME AS parent_table,
               kcu.REFERENCED_COLUMN_NAME AS parent_col
        FROM information_schema.KEY_COLUMN_USAGE kcu
        WHERE kcu.CONSTRAINT_SCHEMA IN ({placeholders})
          AND kcu.REFERENCED_TABLE_SCHEMA IS NOT NULL
          AND kcu.REFERENCED_TABLE_SCHEMA <> kcu.CONSTRAINT_SCHEMA
        """,
        tuple(schema_names),
    )
    out = []
    for r in cur.fetchall():
        out.append(ExecutionIssue(
            severity="error",
            category="cross-schema-fk-violation",
            message=(
                f"FK `{r['name']}` on {r['child_schema']}.{r['child_table']}.{r['child_col']} "
                f"references {r['parent_schema']}.{r['parent_table']}({r['parent_col']}) — a "
                f"foreign key crossing a schema boundary. Once a project is decomposed, "
                f"cross-schema references must be plain documented columns, never FK "
                f"constraints (docs/enterprise_standards_spec.md §2.3)."
            ),
            table=f"{r['child_schema']}.{r['child_table']}",
            object_name=r["name"],
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
    """An unindexed FK column forces a full-table scan on every join through
    it and on every parent-row delete/update integrity check — a real,
    well-documented cost (Percona, MySQL's own optimizer docs on index
    selection), not an imitation of what real-dump tables happen to do.
    Indexing every FK column is kept as the *safe default* here; the
    literature is genuinely split on whether to index FK columns mechanically
    (SQL Server community: yes, proactively; Percona/Karwin's "Index Shotgun":
    only where a query actually uses it) — this check picks the safe-default
    side of that split deliberately, since an unused index is cheap relative
    to a full scan on a live parent-delete. See
    docs/enterprise_standards_spec.md §1.2. Advisory.

    This generator emits its schema with ``SET FOREIGN_KEY_CHECKS = 0`` and
    models most relationships as ``*_id`` columns with a COMMENT rather than a
    declared CONSTRAINT, so the check treats every ``<name>_id`` column as
    FK-intent, not just formal constraints, and a column counts as covered if
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
                    f"{', '.join(uncovered)} — an unindexed FK column forces a full "
                    f"table scan on joins through it and on parent-row delete/update "
                    f"checks. Add KEY idx_{table}_<col> (<col>)."
                ),
                table=table,
            ))
    return out


def _check_missing_timestamps(cols_by_table: dict, fks: list[dict]) -> list[ExecutionIssue]:
    """created_on/modified_on are cheap baseline audit metadata, independent
    of whether a table also gets a full history/audit-log table (that's a
    separate, much more contextual decision — see schema_validator.py's
    data-preservation check and docs/enterprise_standards_spec.md §1.5/§2.5).
    Advisory, not error: omitting them doesn't break anything, it just leaves
    a gap in the audit trail. Pure junction tables are exempt — a pure link
    row rarely needs its own audit trail distinct from the two rows it links.
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
    """MyISAM has no transactions, no row-level locking, no foreign-key
    enforcement, and no crash recovery — this is documented MySQL engine
    behavior (dev.mysql.com storage-engine docs), not a preference inferred
    from how often real dumps happen to use InnoDB. Any schema with FK
    relationships or that needs ACID guarantees is functionally incompatible
    with MyISAM. Error severity."""
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
    """utf8mb4 is the only MySQL charset that stores the full Unicode range
    (emoji, most non-Latin scripts) in a single code path — latin1/utf8mb3
    silently truncate or reject characters outside their range, and mixed
    charsets across tables force a slow conversion on any cross-charset join
    on a string key. This is uncontroversial, current MySQL guidance, not an
    observation about how often real dumps get it wrong. Advisory."""
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
    another index on the same table is dead weight on every write — every
    index adds real write-amplification (each extra index measurably
    increases the cost of every INSERT/UPDATE/DELETE; Winand's
    use-the-index-luke.com: "the first index makes the greatest difference"
    to insert cost, each one after still adds real overhead), so a redundant
    one is pure cost with zero benefit. Advisory."""
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
