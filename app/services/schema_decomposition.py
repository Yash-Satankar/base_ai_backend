# app/services/schema_decomposition.py
"""
Cross-schema DDL handling for a decomposed (multi-schema) project — see
docs/enterprise_standards_spec.md §2.3/§2.4/§5.

Additive: only ever called when a blueprint has ``decomposed=True``. The
existing single-schema path (``planner_helpers.stitch_modules``, one combined
DDL string) is completely untouched by this module's existence.

Real companies that actually run this way (Datadog's migration, Shopify)
forgo cross-schema foreign keys as a discipline choice even where the engine
(MySQL, same-server) would still allow them — a schema boundary meant to be
independently ownable stops using FK constraints across that boundary before
it's physically forced to. This module enforces that convention on
LLM-generated DDL: any FK a batch happened to generate across a schema
boundary is downgraded to a plain, documented reference column.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.services.schema_refiner import _iter_table_blocks, _skip_string
from app.services.planner_helpers import clean_sql

_INLINE_FK_RE = re.compile(
    r"CONSTRAINT\s+[`\"]?(\w+)[`\"]?\s+FOREIGN\s+KEY\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)\s*"
    r"REFERENCES\s+[`\"]?(\w+)[`\"]?\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)"
    r"(?:\s+ON\s+DELETE\s+\w+(?:\s+\w+)?)?(?:\s+ON\s+UPDATE\s+\w+(?:\s+\w+)?)?",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"COMMENT\s*'(?:[^'\\]|\\.)*'", re.IGNORECASE)


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body into its comma-separated field/constraint
    entries — paren- and string-aware, so DECIMAL(10,2) or a COMMENT
    containing a comma is never mistaken for an entry boundary."""
    entries: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c in "'\"`":
            i = _skip_string(body, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            entries.append(body[start:i])
            start = i + 1
        i += 1
    entries.append(body[start:])
    return entries


def _table_body_span(table_text: str) -> tuple[int, int]:
    """Index of the char just after the opening '(' and just before the
    matching closing ')' of a CREATE TABLE block's column/constraint list."""
    open_i = table_text.index("(")
    i = open_i + 1
    depth = 1
    while i < len(table_text) and depth:
        c = table_text[i]
        if c in "'\"`":
            i = _skip_string(table_text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return open_i + 1, i
        i += 1
    return open_i + 1, len(table_text)


def _annotate_column_comment(entry: str, note: str) -> str:
    """Add or replace this field entry's COMMENT with `note`."""
    quoted = "'" + note.replace("'", "''") + "'"
    if _COMMENT_RE.search(entry):
        return _COMMENT_RE.sub(f"COMMENT {quoted}", entry, count=1)
    return entry.rstrip() + f" COMMENT {quoted}"


def _downgrade_cross_schema_fks(table_text: str, own_schema: str,
                                table_owner_schema: dict[str, str]) -> str:
    """Remove any inline FK CONSTRAINT in this table whose referenced parent
    lives in a different schema, and document the reference on that
    column's own definition instead (docs/enterprise_standards_spec.md §2.3)."""
    body_start, body_end = _table_body_span(table_text)
    body = table_text[body_start:body_end]
    entries = _split_top_level(body)

    fk_by_col: dict[str, tuple[str, str]] = {}   # col -> (parent_schema.table, parent_col)
    kept: list[str] = []
    for entry in entries:
        m = _INLINE_FK_RE.search(entry)
        if not m:
            kept.append(entry)
            continue
        _fk_name, col, parent_table, parent_col = m.groups()
        parent_schema = table_owner_schema.get(parent_table.lower())
        if parent_schema is not None and parent_schema != own_schema:
            fk_by_col[col.lower()] = (f"{parent_schema}.{parent_table}", parent_col)
            continue   # drop the CONSTRAINT entry — cross-schema, no FK by design
        kept.append(entry)

    if not fk_by_col:
        return table_text   # nothing crossed a schema boundary — untouched

    for i, entry in enumerate(kept):
        stripped = entry.strip()
        first_token = re.match(r"[`\"]?(\w+)[`\"]?", stripped)
        if not first_token:
            continue
        col = first_token.group(1).lower()
        if col in fk_by_col and not re.match(
            r"(KEY|INDEX|PRIMARY|UNIQUE|CONSTRAINT|FOREIGN)\b", stripped, re.IGNORECASE
        ):
            ref_table, ref_col = fk_by_col[col]
            note = f"References {ref_table}({ref_col}) — cross-schema, no FK by design"
            kept[i] = _annotate_column_comment(entry, note)

    new_body = ",".join(kept)
    return table_text[:body_start] + new_body + table_text[body_end:]


def split_ddl_by_schema(
    all_sql_parts: list[dict],
    project_name: str,
    module_schema_map: dict[str, str],
) -> dict[str, str]:
    """Group generated module SQL into one DDL string per schema-module.

    Any FK crossing a schema boundary is downgraded to a documented plain
    column (see ``_downgrade_cross_schema_fks``); every table gets an
    "Owned by" header comment (docs/enterprise_standards_spec.md §5b).
    Returns ``{schema_name: ddl_text}`` — one entry per distinct schema in
    ``module_schema_map``. Modules with no schema_name (shouldn't happen
    once decomposition is confirmed, but handled defensively) fall into a
    catch-all ``"default"`` schema rather than being silently dropped.
    """
    # table -> owning schema, across ALL modules, before any per-schema split
    table_owner_schema: dict[str, str] = {}
    for part in all_sql_parts:
        schema_name = module_schema_map.get(part["module"], "default")
        for t in part.get("tables", []):
            table_owner_schema[t.lower()] = schema_name

    by_schema: dict[str, list[dict]] = {}
    for part in all_sql_parts:
        schema_name = module_schema_map.get(part["module"], "default")
        by_schema.setdefault(schema_name, []).append(part)

    out: dict[str, str] = {}
    for schema_name, parts in by_schema.items():
        total_tables = sum(len(p["tables"]) for p in parts)
        header = f"""-- ============================================================
-- Project  : {project_name}
-- Schema   : {schema_name}
-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
-- Modules  : {len(parts)}
-- Tables   : {total_tables}
-- ============================================================

SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
SET time_zone = "+00:00";
START TRANSACTION;

"""
        sections = []
        for part in parts:
            module_sql = clean_sql(part["sql"])
            annotated_tables = []
            cursor = 0
            pieces = []
            for name, start, end, text in _iter_table_blocks(module_sql):
                pieces.append(module_sql[cursor:start])
                fixed = _downgrade_cross_schema_fks(text, schema_name, table_owner_schema)
                pieces.append(f"-- Owned by: {schema_name} schema\n{fixed}")
                cursor = end
                annotated_tables.append(name)
            pieces.append(module_sql[cursor:])
            module_sql = "".join(pieces)

            sections.append(f"""
-- ============================================================
-- MODULE: {part['module']}  ({len(part['tables'])} tables)
-- ============================================================

{module_sql}
""")

        footer = "\nCOMMIT;\nSET FOREIGN_KEY_CHECKS = 1;\n"
        out[schema_name] = header + "\n".join(sections) + footer

    return out
