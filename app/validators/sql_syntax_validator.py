import sqlparse

def validate_sql_syntax(sql: str) -> dict:
    try:
        statements = sqlparse.parse(sql)
        errors = []
        for stmt in statements:
            if not stmt.tokens:
                errors.append("Empty statement found")
        return {
            "valid": len(errors) == 0,
            "statement_count": len(statements),
            "errors": errors,
        }
    except Exception as e:
        return {"valid": False, "errors": [str(e)]}