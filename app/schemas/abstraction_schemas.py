# app/schemas/abstraction_schemas.py
"""
Strongly-typed Pydantic v2 schemas representing the intermediate layers
of the 9-Level Abstraction Pipeline.
"""

from typing import Literal, Optional, List
from pydantic import BaseModel, Field


# ── Level 1: Business Understanding ──────────────────────────────────────────

class L1_UnderstandingSpec(BaseModel):
    project_name: str = Field(..., description="Name of the platform or system.")
    business_goal: str = Field(..., description="The main business objective and problem statement.")
    target_users: List[str] = Field(..., description="Target user personas (e.g. Passenger, Admin, Operator).")
    scale: Literal["small", "medium", "large", "enterprise"] = Field(
        "medium",
        description="The scale of operations, which impacts column choices and index tuning."
    )
    domain: str = Field(..., description="The primary business domain (e.g. logistics, healthcare).")
    compliance_requirements: List[str] = Field(
        default_factory=list,
        description="Regulatory or compliance requirements (e.g., HIPAA, GDPR, PCI-DSS, GST)."
    )


# ── Level 2: Business Capabilities ───────────────────────────────────────────

class CapabilityItem(BaseModel):
    name: str = Field(..., description="The capability name (e.g., Double-entry Ledger, Ride Dispatch).")
    description: str = Field(..., description="What this capability allows the business to do.")
    reusable_component_id: Optional[str] = Field(
        None,
        description="ID of the configurable architecture component if applicable (e.g., 'LedgerEngine', 'ApprovalEngine')."
    )
    reasoning: str = Field(..., description="Rationale for why this capability is required.")


class L2_CapabilitySpec(BaseModel):
    capabilities: List[CapabilityItem] = Field(..., description="The list of functional business capabilities.")


# ── Level 3: Business Workflows ──────────────────────────────────────────────

class WorkflowStep(BaseModel):
    step_number: int = Field(..., description="Sequence number of the step.")
    action: str = Field(..., description="The action performed in this step.")
    actor: str = Field(..., description="The persona or service performing the action.")
    entity_affected: str = Field(..., description="The primary business entity impacted by this step.")
    reusable_engine_action: Optional[str] = Field(
        None,
        description="Trigger action on a reusable engine (e.g. 'trigger_approval', 'post_ledger_transaction')."
    )


class WorkflowItem(BaseModel):
    name: str = Field(..., description="Name of the business workflow (e.g., Process Customer Refund).")
    description: str = Field(..., description="Purpose and description of the workflow.")
    steps: List[WorkflowStep] = Field(..., description="The sequential steps of the workflow.")


class L3_WorkflowSpec(BaseModel):
    workflows: List[WorkflowItem] = Field(..., description="The core business workflows.")


# ── Level 4: Business Entities ───────────────────────────────────────────────

class EntityItem(BaseModel):
    name: str = Field(..., description="The singular name of the business entity (e.g., Booking, Invoice).")
    description: str = Field(..., description="What this entity represents.")
    entity_type: Literal["master", "event", "configuration", "detail", "junction", "lookup"] = Field(
        ...,
        description="The type of entity, which maps to table naming taxonomy."
    )
    is_reusable_component_table: bool = Field(
        False,
        description="True if this entity is managed by a pre-built configurable Architecture Component."
    )
    component_owner: Optional[str] = Field(
        None,
        description="The name of the Architecture Component owning this entity (e.g., 'ApprovalEngine')."
    )


class L4_EntitySpec(BaseModel):
    entities: List[EntityItem] = Field(..., description="The core domain business entities.")


# ── Level 5: Relationships ───────────────────────────────────────────────────

class RelationshipItem(BaseModel):
    source_entity: str = Field(..., description="The parent or source entity name.")
    target_entity: str = Field(..., description="The child or target entity name.")
    relationship_type: Literal["1:1", "1:N", "N:M"] = Field(..., description="Relationship cardinality.")
    is_identifying: bool = Field(
        False,
        description="True if the child entity cannot exist without the parent entity."
    )
    description: str = Field(..., description="Explaining the business meaning of this relationship.")


class L5_RelationshipSpec(BaseModel):
    relationships: List[RelationshipItem] = Field(..., description="Entity relationships.")


# ── Level 6: Lifecycle States ────────────────────────────────────────────────

class StateTransition(BaseModel):
    from_state: str = Field(..., description="Origin state.")
    to_state: str = Field(..., description="Destination state.")
    trigger_event: str = Field(..., description="The event that triggers this state change.")
    conditions: List[str] = Field(
        default_factory=list,
        description="Conditions or constraints required for this transition to be valid."
    )


class LifecycleItem(BaseModel):
    entity_name: str = Field(..., description="The entity name (must match an L4 entity).")
    states: List[str] = Field(..., description="List of all possible states.")
    transitions: List[StateTransition] = Field(..., description="Valid transitions in the state machine.")
    requires_state_log_table: bool = Field(
        True,
        description="True if this entity requires a companion _life_cycle_all table for audit trail."
    )


class L6_LifecycleSpec(BaseModel):
    lifecycles: List[LifecycleItem] = Field(..., description="Lifecycle state machines.")


# ── Level 7: Architecture Modules ────────────────────────────────────────────

class ModuleItem(BaseModel):
    name: str = Field(..., description="Name of the module (e.g. Identity, Billing).")
    description: str = Field(..., description="What this module encapsulates.")
    entities: List[str] = Field(..., description="List of L4 entity names contained in this module.")
    workflows: List[str] = Field(..., description="List of L3 workflow names belonging to this module.")
    dependencies: List[str] = Field(
        default_factory=list,
        description="List of other L7 module names that this module depends on."
    )


class L7_ModuleSpec(BaseModel):
    modules: List[ModuleItem] = Field(..., description="High-level logical architecture modules.")
