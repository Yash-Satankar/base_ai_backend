# app/engine/abstraction_pipeline.py
"""
The Abstraction Engine: A step-by-step compiler that translates high-level
business requirements into structured database designs through 9 levels.
"""

import json
import logging
from typing import Tuple, Any
from app.schemas.abstraction_schemas import (
    L1_UnderstandingSpec,
    L2_CapabilitySpec,
    L3_WorkflowSpec,
    L4_EntitySpec,
    L5_RelationshipSpec,
    L6_LifecycleSpec,
    L7_ModuleSpec,
)
from app.schemas.blueprint_schema import BlueprintSpec, BlueprintModuleSpec, BlueprintTableSpec, TableType

logger = logging.getLogger(__name__)


# L1-L8 compile outputs are JSON plans — a few thousand tokens even for a large
# domain, and L5+L6+L7 are compiled together in one call so a large domain's
# combined relationships/lifecycles/modules JSON can run well past that. On
# Groq's free tier this was capped low to stay near its 8000 tokens-per-minute
# window; a cap that small TRUNCATES a big domain's JSON mid-array (an
# "Expecting value" parse error), so only apply it on Groq. Other providers
# (Together, Anthropic, Ollama) have no such per-minute ceiling.
_L_COMPILE_MAX_TOKENS_GROQ = 6000
_L_COMPILE_MAX_TOKENS_DEFAULT = 14000


def generate_schema(system_prompt: str, user_prompt: str, max_tokens: int = None) -> dict:
    """
    Local shim: every L1-L8 compile call goes through the tagged llm_client so
    it is cost-attributed to the conversation (via ambient context set by the
    blueprint job) and counts toward the warn-and-degrade budget.
    """
    from app.conversation.llm_client import call_llm
    from app.core.config import settings
    default_cap = (_L_COMPILE_MAX_TOKENS_GROQ if settings.AI_PROVIDER == "groq"
                  else _L_COMPILE_MAX_TOKENS_DEFAULT)
    return call_llm(
        operation="blueprint_compile",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens or default_cap,
    )


def _clean_and_parse_json(content: str) -> dict:
    """Helper to strip markdown fences and parse JSON."""
    content = content.strip()
    if "```" in content:
        parts = content.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    pass
    return json.loads(content)


# ── L1: Business Understanding ───────────────────────────────────────────────

def generate_l1_understanding(requirement: str) -> L1_UnderstandingSpec:
    """Analyze raw text requirement to extract L1 Business Understanding."""
    system_prompt = """You are an Enterprise Business Analyst.
Analyze the user's raw database requirement and extract the core business parameters.
Return ONLY valid JSON matching this schema:
{
  "project_name": "Name of the system",
  "business_goal": "One sentence primary business objective",
  "target_users": ["User Role A", "User Role B"],
  "scale": "small" or "medium" or "large" or "enterprise",
  "domain": "e_commerce" or "healthcare" or "logistics" or "financial" or "general",
  "compliance_requirements": ["HIPAA", "GDPR", "GST", etc.]
}"""

    response = generate_schema(system_prompt=system_prompt, user_prompt=requirement)
    data = _clean_and_parse_json(response["content"])
    return L1_UnderstandingSpec(**data)


# ── L2: Business Capabilities ────────────────────────────────────────────────

def compile_l1_to_l2(l1: L1_UnderstandingSpec) -> L2_CapabilitySpec:
    """Derive L2 Business Capabilities from L1 Business Understanding."""
    system_prompt = """You are an Enterprise Software Architect.
Given the L1 Business Understanding, identify the core functional capabilities required.
Check if any capability matches our pre-built reusable engines:
- 'LedgerEngine' (for double-entry financial ledger, invoicing, wallets, payroll, balance sheets)
- 'ApprovalEngine' (for multi-stage, role-based workflows, transitions requiring manager approval)
- 'RBACEngine' (for roles, permissions, API keys, organization members)
- 'AuditEngine' (for compliance-heavy tracking, history preservation, change logs)
- 'NotificationEngine' (for alerts, email, SMS, push queue)

Return ONLY valid JSON matching this schema:
{
  "capabilities": [
    {
      "name": "Capability Name",
      "description": "Functional description of what this allows",
      "reusable_component_id": "LedgerEngine" or "ApprovalEngine" or "RBACEngine" or "AuditEngine" or "NotificationEngine" or null,
      "reasoning": "Why this capability is required based on L1"
    }
  ]
}"""

    user_prompt = f"L1 Business Understanding:\n{l1.model_dump_json(indent=2)}"
    response = generate_schema(system_prompt=system_prompt, user_prompt=user_prompt)
    data = _clean_and_parse_json(response["content"])
    return L2_CapabilitySpec(**data)


# ── L3: Business Workflows ───────────────────────────────────────────────────

def compile_l2_to_l3(l1: L1_UnderstandingSpec, l2: L2_CapabilitySpec) -> L3_WorkflowSpec:
    """Map L2 Capabilities to L3 Business Workflows."""
    system_prompt = """You are a Business Process Engineer.
Design the step-by-step workflows (L3) required to support the L2 Capabilities.
Map steps to reusable engine actions where applicable (e.g. 'trigger_approval', 'post_ledger_transaction', 'send_notification').

Return ONLY valid JSON matching this schema:
{
  "workflows": [
    {
      "name": "Workflow Name (e.g., Process Order Placement)",
      "description": "Workflow description",
      "steps": [
        {
          "step_number": 1,
          "action": "What happens in this step",
          "actor": "Who performs this step",
          "entity_affected": "The entity name impacted",
          "reusable_engine_action": "trigger_approval" or "post_ledger_transaction" or "send_notification" or null
        }
      ]
    }
  ]
}"""

    user_prompt = f"L1 Context:\n{l1.model_dump_json(indent=2)}\n\nL2 Capabilities:\n{l2.model_dump_json(indent=2)}"
    response = generate_schema(system_prompt=system_prompt, user_prompt=user_prompt)
    data = _clean_and_parse_json(response["content"])
    return L3_WorkflowSpec(**data)


# ── L4: Business Entities ────────────────────────────────────────────────────

def compile_l3_to_l4(l1: L1_UnderstandingSpec, l3: L3_WorkflowSpec) -> L4_EntitySpec:
    """Extract L4 Business Entities from L3 Workflows."""
    system_prompt = """You are a Data Modeler.
Extract the core Business Entities (L4) needed to store data for the L3 Workflows.
Classify them into:
- 'master': Primary resource (e.g., User, Product)
- 'event': Transaction or log (e.g., Order, Payment)
- 'configuration': Settings (e.g., PricingRule)
- 'detail': Child item (e.g., OrderItem)
- 'junction': Many-to-many link
- 'lookup': Status, Category

Flag if the entity is owned by a reusable component (e.g., LedgerEngine owns LedgerAccount, LedgerTransaction).

Return ONLY valid JSON matching this schema:
{
  "entities": [
    {
      "name": "SingularEntityName (e.g., Flight, Booking)",
      "description": "What this entity represents",
      "entity_type": "master" or "event" or "configuration" or "detail" or "junction" or "lookup",
      "is_reusable_component_table": true or false,
      "component_owner": "LedgerEngine" or "ApprovalEngine" or "RBACEngine" or "AuditEngine" or "NotificationEngine" or null
    }
  ]
}"""

    user_prompt = f"L1 Context:\n{l1.model_dump_json(indent=2)}\n\nL3 Workflows:\n{l3.model_dump_json(indent=2)}"
    response = generate_schema(system_prompt=system_prompt, user_prompt=user_prompt)
    data = _clean_and_parse_json(response["content"])
    return L4_EntitySpec(**data)


# ── L5, L6, L7: Relationships, Lifecycles, Modules ────────────────────────────

_DECOMPOSITION_COUPLING_INSTRUCTION = """
4. Schema decomposition is under consideration for this project. For every
   entry in a module's "dependencies", classify the coupling in
   "dependency_coupling" as:
   - "tight": the depending module cannot function correctly without the
     other in the same transaction (e.g. an order line item cannot be
     correct without its order in the same atomic write) — modules linked
     this way must stay in the same schema.
   - "loose": an eventually-consistent reference (e.g. an order referencing
     a customer, where the order can still function if the customer record
     is briefly stale or unavailable) — modules linked this way are
     eligible to become separate schema boundaries.
   Only classify dependencies that are actually listed; leave
   "dependency_coupling" empty for a module with no dependencies."""

_DECOMPOSITION_COUPLING_SCHEMA_HINT = ',\n      "dependency_coupling": {"OtherModuleName": "tight" or "loose"}'


def compile_l4_to_l5_l6_l7(
    l1: L1_UnderstandingSpec,
    l4: L4_EntitySpec,
    *,
    decomposition_requested: bool = False,
) -> Tuple[L5_RelationshipSpec, L6_LifecycleSpec, L7_ModuleSpec]:
    """Generate L5 (Relationships), L6 (Lifecycles), and L7 (Modules) based on L4 Entities.

    ``decomposition_requested`` is an additive branch, off by default: when
    False (the default, single-schema path) the prompt is byte-identical to
    before schema decomposition existed. Only set True after the user has
    explicitly confirmed they want the project split into separate schemas
    — see docs/enterprise_standards_spec.md §2.2/§2.4.
    """
    system_prompt = """You are a Database Architect.
For the L4 Entities provided:
1. Define L5 Relationships (cardinalities, source, target).
2. Define L6 Lifecycles (state machine transitions for mutable 'master' or 'event' entities).
3. Group entities and workflows into L7 logical Modules.""" + (
        _DECOMPOSITION_COUPLING_INSTRUCTION if decomposition_requested else ""
    ) + """

Return ONLY valid JSON matching this combined schema:
{
  "relationships": [
    {
      "source_entity": "SourceEntity",
      "target_entity": "TargetEntity",
      "relationship_type": "1:1" or "1:N" or "N:M",
      "is_identifying": true or false,
      "description": "Business relationship description"
    }
  ],
  "lifecycles": [
    {
      "entity_name": "EntityName",
      "states": ["STATE_A", "STATE_B"],
      "transitions": [
        {
          "from_state": "STATE_A",
          "to_state": "STATE_B",
          "trigger_event": "User clicks cancel",
          "conditions": ["Order has not shipped"]
        }
      ],
      "requires_state_log_table": true
    }
  ],
  "modules": [
    {
      "name": "Module Name",
      "description": "Module description",
      "entities": ["EntityA", "EntityB"],
      "workflows": ["WorkflowName"],
      "dependencies": ["OtherModuleName"]""" + (
        _DECOMPOSITION_COUPLING_SCHEMA_HINT if decomposition_requested else ""
    ) + """
    }
  ]
}"""

    user_prompt = f"L1 Context:\n{l1.model_dump_json(indent=2)}\n\nL4 Entities:\n{l4.model_dump_json(indent=2)}"
    response = generate_schema(system_prompt=system_prompt, user_prompt=user_prompt)
    data = _clean_and_parse_json(response["content"])

    r_spec = L5_RelationshipSpec(relationships=data.get("relationships", []))
    l_spec = L6_LifecycleSpec(lifecycles=data.get("lifecycles", []))
    m_spec = L7_ModuleSpec(modules=data.get("modules", []))

    return r_spec, l_spec, m_spec


# ── L8: Compile to Physical Database Blueprint ───────────────────────────────

def compile_to_l8_blueprint(
    l1: L1_UnderstandingSpec,
    l4: L4_EntitySpec,
    l5: L5_RelationshipSpec,
    l6: L6_LifecycleSpec,
    l7: L7_ModuleSpec,
) -> BlueprintSpec:
    """
    Compile L1-L7 specifications into a physical L8 BlueprintSpec.
    Applies standard physical naming conventions, adds companion tables (archive/lifecycle),
    and wires up Centralized ID registry (unique_id_header_all).
    """
    modules_list = []
    
    # Track all tables generated to prevent duplicates
    table_names = set()

    # Step 1: Initialize Infrastructure Module
    infra_tables = [
        BlueprintTableSpec(
            name="unique_id_header_all",
            purpose="Centralised business ID registry for tracking every entity across all modules",
            table_type=TableType.HEADER,
            entity_name="unique_id"
        ),
        BlueprintTableSpec(
            name="factory_reset_all",
            purpose="Global platform feature flags and kill-switches singleton",
            table_type=TableType.FACTORY,
            entity_name="factory"
        )
    ]
    modules_list.append(
        BlueprintModuleSpec(
            name="Infrastructure",
            description="System registry, unique ID mapping, and configuration",
            tables=infra_tables,
            dependencies=[]
        )
    )
    table_names.add("unique_id_header_all")
    table_names.add("factory_reset_all")

    # Map L4 Entity Types to Table Types
    type_map = {
        "master": TableType.HEADER,
        "event": TableType.TRANSACTION,
        "configuration": TableType.CONFIGURATION,
        "detail": TableType.DETAILS,
        "junction": TableType.JUNCTION,
        "lookup": TableType.LOOKUP
    }

    # Step 2: Loop through L7 modules and build physical tables
    for m in l7.modules:
        m_tables = []
        
        # Build physical tables for each entity in this module
        for ent_name in m.entities:
            # Find the entity spec from L4
            entity = next((e for e in l4.entities if e.name == ent_name), None)
            if not entity:
                continue

            base_table_name = _to_snake_case(entity.name)
            
            # Apply suffix according to entity type
            suffix = "_header_all" if entity.entity_type == "master" \
                else "_transaction_all" if entity.entity_type == "event" \
                else "_configuration_all" if entity.entity_type == "configuration" \
                else "_details_all" if entity.entity_type == "detail" \
                else "_all"
            
            table_name = f"{base_table_name}{suffix}"

            # Check if this entity has a lifecycle state machine in L6
            lifecycle = next((l for l in l6.lifecycles if l.entity_name == ent_name), None)
            requires_lifecycle = lifecycle.requires_state_log_table if lifecycle else False

            # Header tables default to requiring archiving for historical audit trails
            requires_archive = (entity.entity_type == "master")

            if table_name not in table_names:
                m_tables.append(
                    BlueprintTableSpec(
                        name=table_name,
                        purpose=entity.description,
                        table_type=type_map.get(entity.entity_type, TableType.OTHER),
                        entity_name=base_table_name,
                        requires_archive=requires_archive,
                        requires_lifecycle=requires_lifecycle
                    )
                )
                table_names.add(table_name)

            # Generate companion archive table if required
            if requires_archive:
                archive_name = f"{base_table_name}_archive_all"
                if archive_name not in table_names:
                    m_tables.append(
                        BlueprintTableSpec(
                            name=archive_name,
                            purpose=f"Historical archive mirror of {table_name}",
                            table_type=TableType.ARCHIVE,
                            entity_name=base_table_name
                        )
                    )
                    table_names.add(archive_name)

            # Generate companion lifecycle table if required
            if requires_lifecycle:
                lifecycle_name = f"{base_table_name}_life_cycle_all"
                if lifecycle_name not in table_names:
                    m_tables.append(
                        BlueprintTableSpec(
                            name=lifecycle_name,
                            purpose=f"State transition log and audit trail for {table_name}",
                            table_type=TableType.LIFECYCLE,
                            entity_name=base_table_name
                        )
                    )
                    table_names.add(lifecycle_name)

        if m_tables:
            modules_list.append(
                BlueprintModuleSpec(
                    name=m.name,
                    description=m.description,
                    tables=m_tables,
                    dependencies=m.dependencies
                )
            )

    return BlueprintSpec(
        project_name=l1.project_name,
        description=l1.business_goal,
        domain=l1.domain,
        scale=l1.scale,
        gst_required="gst" in [req.lower() for req in l1.compliance_requirements],
        modules=modules_list
    )


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _to_snake_case(name: str) -> str:
    """Convert PascalCase or CamelCase string to snake_case."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

import re
