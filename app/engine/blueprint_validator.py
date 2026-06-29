# app/engine/blueprint_validator.py
"""
Deterministic validation engine for blueprints.
Validates structural integrity, dependency cycles, naming consistency,
and structural requirements (e.g., three-layer preservation) before any AI calls.
"""

from typing import List
from pydantic import BaseModel, Field
from app.schemas.blueprint_schema import BlueprintSpec, TableType


class BlueprintViolation(BaseModel):
    category: str        # naming | integrity | dependencies | structures
    table_name: str
    message: str
    severity: str        # critical | warning | info
    suggestion: str


class BlueprintValidationResult(BaseModel):
    valid: bool
    score: int          # 0-100 score
    violations: List[BlueprintViolation] = Field(default_factory=list)


class BlueprintValidator:
    """
    Validates a BlueprintSpec against structural database architecture constraints.
    Ensures the blueprint is valid before beginning SQL generation.
    """

    def validate(self, spec: BlueprintSpec) -> BlueprintValidationResult:
        violations: List[BlueprintViolation] = []
        
        # Check 1: Naming taxonomies and snake_case
        self._check_naming(spec, violations)

        # Check 2: Infrastructure / system tables completeness
        self._check_infrastructure(spec, violations)

        # Check 3: Three-layer preservation checks
        self._check_preservation_layers(spec, violations)

        # Check 4: Module dependencies integrity
        self._check_dependencies(spec, violations)

        # Calculate a 100-point score
        score = 100
        for v in violations:
            if v.severity == "critical":
                score -= 15
            elif v.severity == "warning":
                score -= 5
            else:
                score -= 1
        score = max(0, score)

        # Blueprint is valid if there are no critical violations
        is_valid = not any(v.severity == "critical" for v in violations)

        return BlueprintValidationResult(
            valid=is_valid,
            score=score,
            violations=violations
        )

    def _check_naming(self, spec: BlueprintSpec, violations: List[BlueprintViolation]):
        for m in spec.modules:
            for t in m.tables:
                # 1. Enforce lowercase snake_case
                if not t.name.islower() or not all(c.isalnum() or c == '_' for c in t.name):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=t.name,
                            severity="warning",
                            message="Table name is not in lowercase snake_case format.",
                            suggestion=f"Rename '{t.name}' to lowercase snake_case (e.g. '{t.name.lower()}')"
                        )
                    )

                # 2. Check suffix vs table_type mismatch
                name = t.name
                if t.table_type == TableType.HEADER and not name.endswith("_header_all"):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=name,
                            severity="critical",
                            message="Table type is HEADER but name does not end with '_header_all'.",
                            suggestion=f"Rename '{name}' to end with '_header_all'."
                        )
                    )
                elif t.table_type == TableType.TRANSACTION and not name.endswith("_transaction_all"):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=name,
                            severity="critical",
                            message="Table type is TRANSACTION but name does not end with '_transaction_all'.",
                            suggestion=f"Rename '{name}' to end with '_transaction_all'."
                        )
                    )
                elif t.table_type == TableType.ARCHIVE and not name.endswith("_archive_all"):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=name,
                            severity="critical",
                            message="Table type is ARCHIVE but name does not end with '_archive_all'.",
                            suggestion=f"Rename '{name}' to end with '_archive_all'."
                        )
                    )
                elif t.table_type == TableType.LIFECYCLE and not name.endswith("_life_cycle_all"):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=name,
                            severity="critical",
                            message="Table type is LIFECYCLE but name does not end with '_life_cycle_all'.",
                            suggestion=f"Rename '{name}' to end with '_life_cycle_all'."
                        )
                    )
                elif t.table_type == TableType.CONFIGURATION and not name.endswith("_configuration_all"):
                    violations.append(
                        BlueprintViolation(
                            category="naming",
                            table_name=name,
                            severity="warning",
                            message="Table type is CONFIGURATION but name does not end with '_configuration_all'.",
                            suggestion=f"Consider renaming '{name}' to end with '_configuration_all'."
                        )
                    )

    def _check_infrastructure(self, spec: BlueprintSpec, violations: List[BlueprintViolation]):
        # 1. Enforce existence of unique_id_header_all
        all_table_names = {t.name for m in spec.modules for t in m.tables}
        if "unique_id_header_all" not in all_table_names:
            violations.append(
                BlueprintViolation(
                    category="integrity",
                    table_name="unique_id_header_all",
                    severity="critical",
                    message="Missing unique_id_header_all centralized ID registry table.",
                    suggestion="Add 'unique_id_header_all' table under the 'Infrastructure' module."
                )
            )

        # 2. Check if unique_id_header_all is in Infrastructure module
        infra_module = next((m for m in spec.modules if m.name.lower() in ["infrastructure", "system"]), None)
        if infra_module:
            infra_tables = {t.name for t in infra_module.tables}
            if "unique_id_header_all" in all_table_names and "unique_id_header_all" not in infra_tables:
                violations.append(
                    BlueprintViolation(
                        category="integrity",
                        table_name="unique_id_header_all",
                        severity="warning",
                        message="unique_id_header_all exists but is not placed in the Infrastructure module.",
                        suggestion="Move 'unique_id_header_all' table to the 'Infrastructure' module."
                    )
                )

    def _check_preservation_layers(self, spec: BlueprintSpec, violations: List[BlueprintViolation]):
        all_table_names = {t.name for m in spec.modules for t in m.tables}

        for m in spec.modules:
            for t in m.tables:
                if t.table_type == TableType.HEADER:
                    base_name = t.name.replace("_header_all", "")
                    
                    # 1. Check archive table if requested
                    if t.requires_archive:
                        archive_name = f"{base_name}_archive_all"
                        if archive_name not in all_table_names:
                            violations.append(
                                BlueprintViolation(
                                    category="structures",
                                    table_name=t.name,
                                    severity="warning",
                                    message=f"Header table requests archive mirroring, but no companion '{archive_name}' exists in the blueprint.",
                                    suggestion=f"Add '{archive_name}' of type 'ARCHIVE' to module '{m.name}'."
                                )
                            )

                    # 2. Check lifecycle table if requested
                    if t.requires_lifecycle:
                        lifecycle_name = f"{base_name}_life_cycle_all"
                        if lifecycle_name not in all_table_names:
                            violations.append(
                                BlueprintViolation(
                                    category="structures",
                                    table_name=t.name,
                                    severity="warning",
                                    message=f"Header table requests state lifecycle tracking, but no companion '{lifecycle_name}' exists in the blueprint.",
                                    suggestion=f"Add '{lifecycle_name}' of type 'LIFECYCLE' to module '{m.name}'."
                                )
                            )

    def _check_dependencies(self, spec: BlueprintSpec, violations: List[BlueprintViolation]):
        module_names = {m.name for m in spec.modules}
        
        # 1. Validate listed module dependencies exist
        for m in spec.modules:
            for dep in m.dependencies:
                if dep not in module_names:
                    violations.append(
                        BlueprintViolation(
                            category="dependencies",
                            table_name="N/A",
                            severity="warning",
                            message=f"Module '{m.name}' lists non-existent dependency '{dep}'.",
                            suggestion=f"Remove '{dep}' from the dependencies list of module '{m.name}' or add that module."
                        )
                    )

        # 2. Check for cycle detection
        try:
            _ = spec.generation_order
        except Exception as e:
            violations.append(
                BlueprintViolation(
                    category="dependencies",
                    table_name="N/A",
                    severity="critical",
                    message=f"Circular dependencies detected in module specifications: {e}",
                    suggestion="Ensure your module dependencies form a directed acyclic graph (DAG)."
                )
            )
