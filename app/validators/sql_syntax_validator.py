import sqlparse
import re
import logging

logger = logging.getLogger(__name__)

def validate_sql_syntax(sql: str) -> dict:
    """
    Validate that generated SQL is syntactically parseable.
    Returns dict with valid flag and any issues found.
    """
    # Strip markdown fences
    clean = sql.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        clean = "\n".join(lines).strip()

    issues = []

    try:
        statements = sqlparse.parse(clean)

        if not statements:
            issues.append("No SQL statements found in output")
            return {"valid": False, "issues": issues, "statement_count": 0}

        create_count = 0
        for stmt in statements:
            stmt_str = str(stmt).strip().upper()
            if not stmt_str:
                continue

            # Check it's a CREATE TABLE
            if stmt_str.startswith("CREATE TABLE"):
                create_count += 1

            # Check for dangerous statements
            dangerous = ["DROP TABLE", "DELETE FROM", "TRUNCATE",
                        "ALTER TABLE", "INSERT INTO", "UPDATE "]
            for danger in dangerous:
                if danger in stmt_str:
                    issues.append(
                        f"Dangerous statement found: {danger}"
                    )

        if create_count == 0:
            issues.append("No CREATE TABLE statements found")

        # Basic parentheses balance check
        open_p  = clean.count("(")
        close_p = clean.count(")")
        if open_p != close_p:
            issues.append(
                f"Unbalanced parentheses: {open_p} open, {close_p} close"
            )

        return {
            "valid": len(issues) == 0,
            "statement_count": len(statements),
            "create_table_count": create_count,
            "issues": issues,
        }

    except Exception as e:
        logger.error(f"SQL syntax validation error: {e}")
        return {
            "valid": False,
            "issues": [f"Parse error: {str(e)}"],
            "statement_count": 0,
        }