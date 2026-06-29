# app/schemas/blueprint_schema.py
"""
Strongly-typed Pydantic v2 schemas representing a validated database blueprint.
Provides enums for table classifications and topological dependency resolution.
"""

from enum import Enum
from typing import Literal, Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator


class TableType(str, Enum):
    HEADER         = "HEADER"         # Core master entities (_header_all)
    TRANSACTION    = "TRANSACTION"    # Append-only events/activities (_transaction_all)
    ARCHIVE        = "ARCHIVE"        # Historical mirror table (_archive_all)
    LIFECYCLE      = "LIFECYCLE"      # State machine transition log (_life_cycle_all)
    CONFIGURATION  = "CONFIGURATION"  # Settings and business configurations (_configuration_all)
    DETAILS        = "DETAILS"        # Child line items or sub-details (_details_all)
    CALENDAR       = "CALENDAR"       # Monthly calendar assignment slots (_calendar_all)
    FACTORY        = "FACTORY"        # Global feature flags singleton (factory_reset_all)
    LOOKUP         = "LOOKUP"         # Static categories, states, or types
    JUNCTION       = "JUNCTION"       # Many-to-many lookup linkage tables
    OTHER          = "OTHER"          # Catch-all for non-standard custom types


class BlueprintTableSpec(BaseModel):
    name: str = Field(
        ...,
        description="The exact database table name. Must follow taxonomy naming rules."
    )
    purpose: str = Field(
        ...,
        description="Detailed description of what this table stores and how it's used."
    )
    table_type: TableType = Field(
        ...,
        description="Classification of the table for code generation and validation rules."
    )
    entity_name: str = Field(
        ...,
        description="Core domain entity prefix extracted from the table name."
    )
    requires_archive: bool = Field(
        False,
        description="True if this is a master header table that requires a companion _archive_all table."
    )
    requires_lifecycle: bool = Field(
        False,
        description="True if this is a master header table that requires a companion _life_cycle_all state table."
    )
    estimated_columns: int = Field(
        10,
        ge=4,
        le=50,
        description="Estimated number of columns needed to capture all business requirements."
    )

    @field_validator("name")
    @classmethod
    def validate_table_name_suffix(cls, v: str) -> str:
        """Enforce standard naming suffixes for MySQL tables."""
        val = v.lower()
        if not (
            val.endswith("_header_all") or
            val.endswith("_transaction_all") or
            val.endswith("_archive_all") or
            val.endswith("_life_cycle_all") or
            val.endswith("_configuration_all") or
            val.endswith("_details_all") or
            val.endswith("_calendar_all") or
            val.endswith("_all")
        ):
            raise ValueError(f"Table name '{v}' must end with a standard suffix like _header_all, _transaction_all, etc.")
        return val


class BlueprintModuleSpec(BaseModel):
    name: str = Field(
        ...,
        description="Name of the functional grouping or service module (e.g. Inventory Management)"
    )
    description: str = Field(
        ...,
        description="Clear description of the functional scope of this module."
    )
    tables: List[BlueprintTableSpec] = Field(
        ...,
        description="List of tables contained within this module."
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="Names of other modules that must be generated BEFORE this module (for foreign keys)."
    )


class BlueprintSpec(BaseModel):
    project_name: str = Field(
        ...,
        description="Name of the platform or system being designed."
    )
    description: str = Field(
        ...,
        description="High-level description of what the platform does."
    )
    version: str = Field(
        "1.0",
        description="Version string of the architecture design."
    )
    domain: str = Field(
        ...,
        description="The primary business domain detected (e.g. logistics, healthcare)."
    )
    gst_required: bool = Field(
        False,
        description="True if the business domain has GST invoicing or tax collection compliance requirements."
    )
    scale: Literal["small", "medium", "large", "enterprise"] = Field(
        "medium",
        description="Scale of operations, which dictates column sizing (INT vs BIGINT) and index strategies."
    )
    modules: List[BlueprintModuleSpec] = Field(
        ...,
        description="Functional modules forming the database architecture."
    )

    @property
    def total_tables(self) -> int:
        return sum(len(m.tables) for m in self.modules)

    @property
    def generation_order(self) -> List[str]:
        """
        Resolves modules in dependency order using topological sorting.
        Ensures that tables containing foreign keys are generated after their parent tables.
        """
        # Build dependency graph
        graph = {m.name: set(m.dependencies) for m in self.modules}
        resolved = []
        unresolved = []

        def visit(node: str):
            if node in unresolved:
                # Cycle detected. Break cycle gracefully in logger, fallback to default order.
                return
            if node not in resolved:
                unresolved.append(node)
                # Ensure all dependency nodes are visited
                for dep in graph.get(node, set()):
                    if dep in graph:  # only visit dependencies within our graph
                        visit(dep)
                unresolved.remove(node)
                resolved.append(node)

        for m_name in graph:
            visit(m_name)

        return resolved

    @model_validator(mode="after")
    def validate_no_duplicate_tables(self) -> 'BlueprintSpec':
        """Ensure no table name is duplicated across modules."""
        table_names = set()
        for m in self.modules:
            for t in m.tables:
                if t.name in table_names:
                    raise ValueError(f"Duplicate table name found across modules: {t.name}")
                table_names.add(t.name)
        return self
