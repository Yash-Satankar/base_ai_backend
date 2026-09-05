"""
Tests for the schema-decomposition additions to BlueprintSpec/BlueprintModuleSpec
(app/schemas/blueprint_schema.py) — see docs/enterprise_standards_spec.md §2.2/§2.4.

Both new fields (BlueprintModuleSpec.schema_name, BlueprintSpec.decomposed)
are optional with backward-compatible defaults — a blueprint built the way
every existing caller already builds one is unaffected.
"""

from app.schemas.blueprint_schema import BlueprintSpec, BlueprintModuleSpec, BlueprintTableSpec, TableType


def _table(name: str) -> BlueprintTableSpec:
    return BlueprintTableSpec(
        name=name, purpose="test", table_type=TableType.HEADER, entity_name="Thing",
    )


def test_module_without_schema_name_defaults_to_none():
    m = BlueprintModuleSpec(name="Mod", description="d", tables=[_table("thing_header_all")])
    assert m.schema_name is None


def test_blueprint_without_decomposition_defaults_are_unaffected():
    bp = BlueprintSpec(
        project_name="P", description="d", domain="logistics",
        modules=[BlueprintModuleSpec(name="Mod", description="d", tables=[_table("thing_header_all")])],
    )
    assert bp.decomposed is False
    assert bp.schema_names == []
    assert bp.total_tables == 1


def test_decomposed_blueprint_reports_distinct_schema_names():
    bp = BlueprintSpec(
        project_name="P", description="d", domain="healthcare",
        decomposed=True,
        modules=[
            BlueprintModuleSpec(name="Clinical", description="d", schema_name="clinical_records",
                                 tables=[_table("patient_header_all")]),
            BlueprintModuleSpec(name="Billing", description="d", schema_name="billing",
                                 tables=[_table("invoice_header_all")]),
            BlueprintModuleSpec(name="ClinicalOrders", description="d", schema_name="clinical_records",
                                 tables=[_table("order_header_all")]),
        ],
    )
    assert bp.decomposed is True
    assert bp.schema_names == ["billing", "clinical_records"]
