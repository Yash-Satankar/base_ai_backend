# app/engine/time_machine.py
"""
Time Machine & Migrations Engine: Compares two blueprint versions
and compiles the DDL SQL migration script.
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def generate_migration_plan(old_blueprint: dict, new_blueprint: dict) -> dict:
    """
    Compares two blueprints and compiles the DDL migration script.
    """
    logger.info("⏳ Compiling schema migration plan...")

    old_tables = {t["name"]: t for m in old_blueprint.get("modules", []) for t in m.get("tables", [])}
    new_tables = {t["name"]: t for m in new_blueprint.get("modules", []) for t in m.get("tables", [])}

    added_tables = []
    removed_tables = []
    modified_tables = {}
    ddl_statements = []

    # 1. Detect Added and Modified Tables
    for t_name, new_table in new_tables.items():
        if t_name not in old_tables:
            added_tables.append(t_name)
            # Compile CREATE TABLE statement (simplified mockup for migration)
            cols = []
            for col in new_table.get("columns", []):
                cols.append(f"  {col['name']} {col['type']}")
            col_lines = ",\n".join(cols)
            ddl_statements.append(
                f"-- Added Table: {t_name}\n"
                f"CREATE TABLE {t_name} (\n"
                f"{col_lines}\n"
                f");"
            )
        else:
            old_table = old_tables[t_name]
            # Compare columns
            old_cols = {c["name"]: c for c in old_table.get("columns", [])}
            new_cols = {c["name"]: c for c in new_table.get("columns", [])}

            added_cols = []
            removed_cols = []

            for c_name, new_col in new_cols.items():
                if c_name not in old_cols:
                    added_cols.append(new_col)
                    ddl_statements.append(
                        f"ALTER TABLE {t_name} ADD COLUMN {c_name} {new_col['type']};"
                    )
            
            for c_name in old_cols:
                if c_name not in new_cols:
                    removed_cols.append(c_name)
                    ddl_statements.append(
                        f"ALTER TABLE {t_name} DROP COLUMN {c_name};"
                    )

            if added_cols or removed_cols:
                modified_tables[t_name] = {
                    "added_columns": [c["name"] for c in added_cols],
                    "removed_columns": removed_cols
                }

    # 2. Detect Removed Tables
    for t_name in old_tables:
        if t_name not in new_tables:
            removed_tables.append(t_name)
            ddl_statements.append(
                f"DROP TABLE IF EXISTS {t_name};"
            )

    migration_sql = "\n\n".join(ddl_statements) if ddl_statements else "-- No migration required. Schemas are identical."

    return {
        "migration_sql": migration_sql,
        "added_tables": added_tables,
        "removed_tables": removed_tables,
        "modified_tables": modified_tables,
        "is_empty": len(ddl_statements) == 0
    }
