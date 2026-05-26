# app/validators/schema_validator.py

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ValidationIssue:
    rule_id: int
    rule_name: str
    severity: str        # critical | high | medium | low
    issue: str
    suggestion: str
    table_name: Optional[str] = None


@dataclass
class ValidationResult:
    score: int                              # 0-100
    passed: bool                            # True if score >= 60
    total_issues: int
    critical_issues: int
    high_issues: int
    medium_issues: int
    issues: list[ValidationIssue] = field(default_factory=list)
    scores_breakdown: dict = field(default_factory=dict)
    tables_found: list[str] = field(default_factory=list)
    summary: str = ""


class SchemaValidator:
    """
    Validates generated MySQL schema against the 109 rules.
    Returns a score (0-100) and a list of violations.
    """

    # Scoring dimensions — must sum to 100
    SCORE_WEIGHTS = {
        "naming_convention":    20,
        "audit_fields":         20,
        "financial_compliance": 15,
        "data_preservation":    15,
        "index_constraints":    10,
        "status_convention":    10,
        "identity_system":      10,
    }
    SYSTEM_TABLES = {
        "unique_id_header_all",
        "factory_reset_all",
    }

    def validate(self, sql: str) -> ValidationResult:
        """
        Main entry point.
        Pass the raw SQL DDL string, get back a ValidationResult.
        """
        issues = []

        # Extract table names from SQL
        tables = self._extract_table_names(sql)

        # Run all checks
        issues += self._check_naming_convention(sql, tables)
        issues += self._check_audit_fields(sql, tables)
        issues += self._check_financial_compliance(sql)
        issues += self._check_data_preservation(sql, tables)
        issues += self._check_index_constraints(sql)
        issues += self._check_status_convention(sql)
        issues += self._check_identity_system(sql)
        issues += self._check_money_types(sql)
        issues += self._check_archive_tables(sql, tables)
        issues += self._check_fk_naming(sql)

        # Calculate score
        scores = self._calculate_scores(issues)
        total_score = sum(scores.values())

        critical = sum(1 for i in issues if i.severity == "critical")
        high     = sum(1 for i in issues if i.severity == "high")
        medium   = sum(1 for i in issues if i.severity == "medium")

        summary = self._build_summary(total_score, issues, tables)

        return ValidationResult(
            score=total_score,
            passed=total_score >= 60,
            total_issues=len(issues),
            critical_issues=critical,
            high_issues=high,
            medium_issues=medium,
            issues=issues,
            scores_breakdown=scores,
            tables_found=tables,
            summary=summary,
        )

    # ── Checks ──────────────────────────────────────────────────

    def _check_naming_convention(self, sql: str, tables: list[str]) -> list[ValidationIssue]:
        issues = []
        valid_suffixes = [
            "_header_all", "_details_all", "_transaction_all",
            "_configuration_all", "_archive_all", "_life_cycle_all",
            "_all",
        ]

        for table in tables:
            # Skip framework tables
            if self._is_framework_table(table):
                continue

            has_valid_suffix = any(table.endswith(s) for s in valid_suffixes)
            if not has_valid_suffix:
                issues.append(ValidationIssue(
                    rule_id=1,
                    rule_name="Naming Taxonomy",
                    severity="high",
                    issue=f"Table '{table}' does not follow naming convention",
                    suggestion=f"Rename to '{table}_header_all' or '{table}_transaction_all' etc.",
                    table_name=table,
                ))

        return issues

    def _check_audit_fields(self, sql: str, tables: list[str]) -> list[ValidationIssue]:
        issues = []
        table_blocks = self._split_into_table_blocks(sql)

        for table, block in table_blocks.items():
            # Skip framework AND system tables
            if self._is_framework_table(table) or table in self.SYSTEM_TABLES:
                continue

            block_lower = block.lower()

            if "created_on" not in block_lower:
                issues.append(ValidationIssue(
                    rule_id=30,
                    rule_name="Timestamp Column Naming",
                    severity="high",
                    issue=f"Table '{table}' missing 'created_on' column",
                    suggestion="Add: created_on DATETIME NOT NULL",
                    table_name=table,
                ))

            if "modified_on" not in block_lower:
                issues.append(ValidationIssue(
                    rule_id=30,
                    rule_name="Timestamp Column Naming",
                    severity="high",
                    issue=f"Table '{table}' missing 'modified_on' column",
                    suggestion="Add: modified_on DATETIME NOT NULL",
                    table_name=table,
                ))

            if "status" not in block_lower:
                issues.append(ValidationIssue(
                    rule_id=8,
                    rule_name="Status Integer Convention",
                    severity="medium",
                    issue=f"Table '{table}' missing 'status' column",
                    suggestion="Add: status INT NOT NULL COMMENT '1=active,2=inactive'",
                    table_name=table,
                ))

        return issues

    def _check_financial_compliance(self, sql: str) -> list[ValidationIssue]:
        issues = []
        sql_lower = sql.lower()

        # Check SGST/CGST if financial keywords present
        financial_keywords = ["amount", "price", "fee", "salary", "payment", "invoice"]
        has_financial = any(kw in sql_lower for kw in financial_keywords)

        if has_financial:
            if "sgst_amount" not in sql_lower and "sgst" not in sql_lower:
                issues.append(ValidationIssue(
                    rule_id=7,
                    rule_name="Indian GST Compliance",
                    severity="critical",
                    issue="Financial schema missing 'sgst_amount' column",
                    suggestion="Add sgst_amount DECIMAL(10,2) to transaction tables",
                ))

            if "cgst_amount" not in sql_lower and "cgst" not in sql_lower:
                issues.append(ValidationIssue(
                    rule_id=7,
                    rule_name="Indian GST Compliance",
                    severity="critical",
                    issue="Financial schema missing 'cgst_amount' column",
                    suggestion="Add cgst_amount DECIMAL(10,2) to transaction tables",
                ))

            if "closing_balance" not in sql_lower and "balance" not in sql_lower:
                issues.append(ValidationIssue(
                    rule_id=27,
                    rule_name="Denormalised Running Balance",
                    severity="high",
                    issue="Transaction table missing 'closing_balance' column",
                    suggestion="Add closing_balance DECIMAL(12,2) to track running balance",
                ))

        return issues

    def _check_money_types(self, sql: str) -> list[ValidationIssue]:
        issues = []

        # Find money columns using float or double
        money_keywords = ["amount", "price", "fee", "salary", "balance", "rate", "cost"]
        lines = sql.split("\n")

        for line in lines:
            line_lower = line.lower().strip()
            has_money_word = any(kw in line_lower for kw in money_keywords)
            has_bad_type = re.search(r'\b(float|double)\b', line_lower)

            if has_money_word and has_bad_type:
                issues.append(ValidationIssue(
                    rule_id=29,
                    rule_name="Decimal Money Fields",
                    severity="critical",
                    issue=f"Money column using FLOAT/DOUBLE: {line.strip()[:80]}",
                    suggestion="Use DECIMAL(10,2) for all monetary columns",
                ))

        return issues

    def _check_data_preservation(self, sql: str, tables: list[str]) -> list[ValidationIssue]:
        issues = []
        header_tables = [t for t in tables if t.endswith("_header_all")]

        for ht in header_tables:
            # Skip system tables
            if ht in self.SYSTEM_TABLES:
                continue

            base = ht.replace("_header_all", "")
            archive = f"{base}_archive_all"
            if archive not in tables:
                issues.append(ValidationIssue(
                    rule_id=3,
                    rule_name="Three-Layer Data Preservation",
                    severity="medium",
                    issue=f"'{ht}' has no corresponding archive table '{archive}'",
                    suggestion=f"Create '{archive}' as historical mirror of '{ht}'",
                    table_name=ht,
                ))

        return issues

    def _check_index_constraints(self, sql: str) -> list[ValidationIssue]:
        issues = []

        # Check for anonymous indexes — INDEX (col) without a name
        anonymous_index = re.findall(
            r'\bINDEX\s*\(\s*\w+\s*\)',
            sql, re.IGNORECASE
        )
        for match in anonymous_index:
            issues.append(ValidationIssue(
                rule_id=32,
                rule_name="Named Indexes",
                severity="medium",
                issue=f"Anonymous index found: {match}",
                suggestion="Use: INDEX idx_tablename_columnname (column)",
            ))

        # Check for anonymous foreign keys
        fk_pattern = re.findall(
            r'FOREIGN KEY\s*\([^)]+\)',
            sql, re.IGNORECASE
        )
        constraint_pattern = re.findall(
            r'CONSTRAINT\s+\w+\s+FOREIGN KEY',
            sql, re.IGNORECASE
        )

        if len(fk_pattern) > len(constraint_pattern):
            diff = len(fk_pattern) - len(constraint_pattern)
            issues.append(ValidationIssue(
                rule_id=31,
                rule_name="Named FK Constraints",
                severity="medium",
                issue=f"{diff} foreign key(s) missing CONSTRAINT name",
                suggestion="Use: CONSTRAINT fk_child_parent FOREIGN KEY (...)",
            ))

        return issues

    def _check_status_convention(self, sql: str) -> list[ValidationIssue]:
        issues = []

        # Find status columns NOT using INT
        bad_status = re.findall(
            r'`?status`?\s+(varchar|enum|bool|boolean|tinyint\s*\(\s*[2-9])',
            sql, re.IGNORECASE
        )
        for match in bad_status:
            issues.append(ValidationIssue(
                rule_id=8,
                rule_name="Status Integer Convention",
                severity="medium",
                issue=f"Status column using non-integer type: {match}",
                suggestion="Use INT with COMMENT documenting all values",
            ))

        # Find status INT columns WITHOUT a COMMENT
        status_lines = [
            line for line in sql.split("\n")
            if re.search(r'`?status`?\s+int', line, re.IGNORECASE)
        ]
        for line in status_lines:
            if "COMMENT" not in line.upper():
                issues.append(ValidationIssue(
                    rule_id=8,
                    rule_name="Status Integer Convention",
                    severity="low",
                    issue=f"Status INT column missing COMMENT: {line.strip()[:80]}",
                    suggestion="Add COMMENT '1=active,2=inactive,...' to document values",
                ))

        return issues

    def _check_identity_system(self, sql: str) -> list[ValidationIssue]:
        issues = []
        sql_lower = sql.lower()

        # Check for unique_id_header_all
        if "unique_id_header_all" not in sql_lower:
            issues.append(ValidationIssue(
                rule_id=9,
                rule_name="Centralised ID Registry",
                severity="high",
                issue="Schema missing 'unique_id_header_all' table",
                suggestion="Add unique_id_header_all table for centralised business ID generation",
            ))

        # Check for AUTO_INCREMENT on every table
        table_blocks = self._split_into_table_blocks(sql)
        for table, block in table_blocks.items():
            if self._is_framework_table(table):
                continue
            if "auto_increment" not in block.lower():
                issues.append(ValidationIssue(
                    rule_id=2,
                    rule_name="Two-ID System",
                    severity="high",
                    issue=f"Table '{table}' missing AUTO_INCREMENT primary key",
                    suggestion="Add: id INT AUTO_INCREMENT PRIMARY KEY",
                    table_name=table,
                ))

        return issues

    def _check_archive_tables(self, sql: str, tables: list[str]) -> list[ValidationIssue]:
        issues = []

        archive_tables = [t for t in tables if "_archive_all" in t]

        for at in archive_tables:
            # Archive tables should NOT have UNIQUE constraints
            table_blocks = self._split_into_table_blocks(sql)
            block = table_blocks.get(at, "")
            if re.search(r'\bUNIQUE\b', block, re.IGNORECASE):
                issues.append(ValidationIssue(
                    rule_id=3,
                    rule_name="Three-Layer Data Preservation",
                    severity="high",
                    issue=f"Archive table '{at}' has UNIQUE constraint — will break historical inserts",
                    suggestion="Remove UNIQUE constraints from archive tables",
                    table_name=at,
                ))

        return issues

    def _check_fk_naming(self, sql: str) -> list[ValidationIssue]:
        issues = []

        # FKs referencing business ID column instead of id PK
        bad_fk = re.findall(
            r'REFERENCES\s+\w+\s*\(\s*(?!id\b)(\w+_id|[a-z]+_no)\s*\)',
            sql, re.IGNORECASE
        )
        for match in bad_fk:
            issues.append(ValidationIssue(
                rule_id=31,
                rule_name="Named FK Constraints",
                severity="medium",
                issue=f"FK references business column '{match}' instead of 'id' PK",
                suggestion="Foreign keys must reference the integer 'id' PRIMARY KEY column",
            ))

        return issues

    # ── Scoring ─────────────────────────────────────────────────

    def _calculate_scores(self, issues: list[ValidationIssue]) -> dict:
        """
        Start from max score per dimension.
        Deduct points per issue based on severity.
        """
        DEDUCTIONS = {
            "critical": 8,
            "high":     4,
            "medium":   2,
            "low":      1,
        }

        # Map rule_ids to scoring dimensions
        RULE_TO_DIMENSION = {
            "naming_convention":    [1, 36],
            "audit_fields":         [18, 30, 8],
            "financial_compliance": [7, 27, 29, 51, 57],
            "data_preservation":    [3, 20, 43],
            "index_constraints":    [31, 32, 33, 77],
            "status_convention":    [8, 17, 56],
            "identity_system":      [2, 9, 99],
        }

        # Reverse map
        rule_to_dim = {}
        for dim, rule_ids in RULE_TO_DIMENSION.items():
            for rid in rule_ids:
                rule_to_dim[rid] = dim

        scores = {dim: weight for dim, weight in self.SCORE_WEIGHTS.items()}

        for issue in issues:
            dim = rule_to_dim.get(issue.rule_id, "audit_fields")
            deduction = DEDUCTIONS.get(issue.severity, 2)
            scores[dim] = max(0, scores[dim] - deduction)

        return scores

    # ── Helpers ─────────────────────────────────────────────────

    def _extract_table_names(self, sql: str) -> list[str]:
        matches = re.findall(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
            sql, re.IGNORECASE
        )
        return list(dict.fromkeys(matches))  # deduplicate preserving order

    def _split_into_table_blocks(self, sql: str) -> dict[str, str]:
        """Split full SQL into per-table blocks."""
        blocks = {}
        pattern = re.finditer(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?\s*\((.*?)\)\s*(?:ENGINE|;)',
            sql, re.IGNORECASE | re.DOTALL
        )
        for match in pattern:
            table_name = match.group(1)
            table_body = match.group(2)
            blocks[table_name] = table_body
        return blocks

    def _is_framework_table(self, table_name: str) -> bool:
        framework_tables = {
            "migrations", "jobs", "job_batches", "failed_jobs",
            "sessions", "cache", "cache_locks",
            "password_reset_tokens", "personal_access_tokens", "users",
        }
        return table_name.lower() in framework_tables

    def _build_summary(
        self, score: int, issues: list[ValidationIssue], tables: list[str]
    ) -> str:
        grade = (
            "A" if score >= 90 else
            "B" if score >= 80 else
            "C" if score >= 70 else
            "D" if score >= 60 else
            "F"
        )

        critical = sum(1 for i in issues if i.severity == "critical")
        high     = sum(1 for i in issues if i.severity == "high")
        medium   = sum(1 for i in issues if i.severity == "medium")

        status = "✅ PASSED" if score >= 60 else "❌ FAILED"

        return (
            f"{status} | Grade: {grade} | Score: {score}/100 | "
            f"Tables: {len(tables)} | "
            f"Issues: {critical} critical, {high} high, {medium} medium"
        )