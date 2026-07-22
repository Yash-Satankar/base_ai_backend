# app/db/models.py
"""
SQLAlchemy async ORM models for the persistent project layer.

These are the platform-level entities stored in PostgreSQL.
Everything below is the foundation for: project history, team collaboration,
billing, API key management, and schema versioning.

Design principles:
- Every entity has UUID primary key (not serial int) — globally unique, safe to expose in URLs
- Soft deletion everywhere — no hard deletes from production data
- All timestamps in UTC
- FK relationships lazy-loaded by default; use selectinload() explicitly
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String, Text, Integer, Float, Boolean, DateTime, ForeignKey,
    Enum as SAEnum, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Base ─────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────────────

class ProjectStatus(str, enum.Enum):
    ACTIVE    = "active"
    ARCHIVED  = "archived"
    DELETED   = "deleted"


class VersionStatus(str, enum.Enum):
    DRAFT       = "draft"       # blueprint created, SQL not yet generated
    GENERATING  = "generating"  # SQL generation in progress
    COMPLETE    = "complete"    # SQL generated and validated
    FAILED      = "failed"      # generation failed


class MemberRole(str, enum.Enum):
    OWNER       = "owner"
    EDITOR      = "editor"
    VIEWER      = "viewer"


# ── User ─────────────────────────────────────────────────────────

class User(Base):
    """Platform user. Currently single-user; ready for multi-tenant expansion."""
    __tablename__ = "users"

    id:           Mapped[str]            = mapped_column(String(36), primary_key=True, default=_uuid)
    email:        Mapped[str]            = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str]            = mapped_column(String(100), nullable=False, default="")
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active:    Mapped[bool]           = mapped_column(Boolean, default=True, nullable=False)
    is_verified:  Mapped[bool]           = mapped_column(Boolean, default=False, nullable=False)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Relationships
    api_keys:   Mapped[list["ApiKey"]]   = relationship("ApiKey",   back_populates="user", lazy="noload")
    projects:   Mapped[list["Project"]]  = relationship("Project",  back_populates="owner", lazy="noload",
                                                         foreign_keys="Project.owner_id")

    __table_args__ = (
        Index("idx_users_email", "email"),
    )


# ── API Key ──────────────────────────────────────────────────────

class ApiKey(Base):
    """
    API keys for programmatic access. Hashed at rest, never stored in plaintext.
    Supports per-key rate limits and expiry for enterprise customers.
    """
    __tablename__ = "api_keys"

    id:           Mapped[str]            = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id:      Mapped[str]            = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    name:         Mapped[str]            = mapped_column(String(100), nullable=False)         # "Production key", "CI key"
    key_prefix:   Mapped[str]            = mapped_column(String(12), nullable=False)          # First 8 chars for identification
    key_hash:     Mapped[str]            = mapped_column(String(255), nullable=False)         # bcrypt hash
    is_active:    Mapped[bool]           = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="api_keys")

    __table_args__ = (
        Index("idx_api_keys_user_id", "user_id"),
        Index("idx_api_keys_prefix", "key_prefix"),
    )


# ── Project ──────────────────────────────────────────────────────

class Project(Base):
    """
    A Project is the top-level entity that persists forever.
    It can have many versions, each representing a distinct schema generation.
    Users return to projects across sessions; teams collaborate on shared projects.
    """
    __tablename__ = "projects"

    id:            Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id:      Mapped[str]           = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    name:          Mapped[str]           = mapped_column(String(255), nullable=False)
    description:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    domain:        Mapped[Optional[str]] = mapped_column(String(100), nullable=True)    # detected domain
    status:        Mapped[ProjectStatus] = mapped_column(SAEnum(ProjectStatus), default=ProjectStatus.ACTIVE, nullable=False)
    is_public:     Mapped[bool]          = mapped_column(Boolean, default=False, nullable=False)

    # Aggregate counters (RULE 38 — never COUNT() at read time)
    version_count:      Mapped[int]      = mapped_column(Integer, default=0, nullable=False)
    latest_score:       Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0-100
    latest_table_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Relationships
    owner:    Mapped["User"]                 = relationship("User", back_populates="projects", foreign_keys=[owner_id])
    versions: Mapped[list["ProjectVersion"]] = relationship("ProjectVersion", back_populates="project",
                                                             order_by="desc(ProjectVersion.version_number)", lazy="noload")
    conversations: Mapped[list["Conversation"]] = relationship("Conversation", back_populates="project", lazy="noload")

    __table_args__ = (
        Index("idx_projects_owner_id", "owner_id"),
        Index("idx_projects_status", "status"),
        Index("idx_projects_domain", "domain"),
    )


# ── Project Version ───────────────────────────────────────────────

class ProjectVersion(Base):
    """
    A specific immutable snapshot of a project's architecture.
    Every generation creates a new version. Versions are never mutated.
    This is the git-commit equivalent for database schemas.
    """
    __tablename__ = "project_versions"

    id:             Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:     Mapped[str]           = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
    version_number: Mapped[int]           = mapped_column(Integer, nullable=False)          # 1, 2, 3, ...
    status:         Mapped[VersionStatus] = mapped_column(SAEnum(VersionStatus), default=VersionStatus.DRAFT, nullable=False)

    # Blueprint (the approved architecture plan — stored before SQL is generated)
    blueprint:      Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)             # BlueprintSpec as JSON (L8)
    requirement_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # Original user requirement

    # Intermediate Abstraction Levels (L1 - L7)
    l1_understanding: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l2_capabilities:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l3_workflows:     Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l4_entities:      Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l5_relationships: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l6_lifecycles:    Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    l7_modules:       Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Generated artifacts
    schema_sql:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)             # Full generated SQL DDL (L9)
    sql_file_path:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_file_path:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Validation results
    validation_score:    Mapped[Optional[int]] = mapped_column(Integer, nullable=True)      # 0-100
    validation_grade:    Mapped[Optional[str]] = mapped_column(String(2), nullable=True)    # A, B, C, D, F
    validation_report:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)        # Full validation JSON
    traceability_graph:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)        # Traceability metadata mapping SQL to L1-L7

    # Generation metadata
    domain:              Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    scale:               Mapped[Optional[str]]  = mapped_column(String(20), nullable=True)
    gst_required:        Mapped[bool]           = mapped_column(Boolean, default=False, nullable=False)
    modules_count:       Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    tables_count:        Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    rules_applied_count: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    token_usage:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)        # {input, output, total}
    generation_time_sec: Mapped[Optional[float]] = mapped_column(Integer, nullable=True)
    generation_error:    Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    # Diff metadata (compared to previous version)
    diff_from_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    diff_summary:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Authorship
    created_by:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    # Relationships
    project:     Mapped["Project"]          = relationship("Project", back_populates="versions")
    conversation: Mapped[Optional["Conversation"]] = relationship("Conversation", back_populates="version",
                                                                   uselist=False, lazy="noload")

    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_version_per_project"),
        Index("idx_project_versions_project_id", "project_id"),
        Index("idx_project_versions_status", "status"),
        Index("idx_project_versions_created_at", "created_at"),
    )


# ── Conversation ──────────────────────────────────────────────────

class Conversation(Base):
    """
    A conversation session. Previously ephemeral (Redis only).
    Now persisted to PostgreSQL and linked to a project version.
    Redis still holds the live ConversationState object during active sessions;
    PostgreSQL holds the permanent historical record.
    """
    __tablename__ = "conversations"

    id:             Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:     Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    version_id:     Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("project_versions.id"), nullable=True)
    user_id:        Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    # Session linkage (bridges Redis session to DB record)
    redis_session_id: Mapped[Optional[str]] = mapped_column(String(36), unique=True, nullable=True)

    # Current stage
    stage:          Mapped[Optional[str]] = mapped_column(String(50), nullable=True)        # INITIAL, CLARIFYING, etc.
    message_count:  Mapped[int]           = mapped_column(Integer, default=0, nullable=False)

    started_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    last_active_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    # Relationships
    project:  Mapped[Optional["Project"]]        = relationship("Project", back_populates="conversations")
    version:  Mapped[Optional["ProjectVersion"]] = relationship("ProjectVersion", back_populates="conversation")
    messages: Mapped[list["ConversationMessage"]] = relationship("ConversationMessage", back_populates="conversation",
                                                                  order_by="ConversationMessage.sequence", lazy="noload")

    __table_args__ = (
        Index("idx_conversations_project_id", "project_id"),
        Index("idx_conversations_user_id", "user_id"),
        Index("idx_conversations_redis_session_id", "redis_session_id"),
    )


# ── Conversation Message ──────────────────────────────────────────

class ConversationMessage(Base):
    """
    Individual messages within a conversation.
    Persisted to allow conversation replay, analytics, and fine-tuning dataset export.
    """
    __tablename__ = "conversation_messages"

    id:              Mapped[str]  = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str]  = mapped_column(String(36), ForeignKey("conversations.id"), nullable=False)
    sequence:        Mapped[int]  = mapped_column(Integer, nullable=False)                  # message order
    role:            Mapped[str]  = mapped_column(String(20), nullable=False)               # "user" | "assistant" | "system"
    content:         Mapped[str]  = mapped_column(Text, nullable=False)
    msg_metadata:    Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)            # stage, confidence, round, etc.
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        Index("idx_conv_messages_conversation_id", "conversation_id"),
        Index("idx_conv_messages_sequence", "conversation_id", "sequence"),
    )


# ── Usage Log ────────────────────────────────────────────────────

class UsageLog(Base):
    """
    Every AI generation call is logged for billing, analytics, and abuse detection.
    This is the foundation for usage-based pricing.
    """
    __tablename__ = "usage_logs"

    id:            Mapped[str]  = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id:       Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    project_id:    Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    version_id:    Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    operation:     Mapped[str]  = mapped_column(String(50), nullable=False)                # "generate_schema", "fix_pass", "blueprint"
    ai_provider:   Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ai_model:      Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    input_tokens:  Mapped[int]  = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int]  = mapped_column(Integer, default=0, nullable=False)
    duration_sec:  Mapped[Optional[float]] = mapped_column(Integer, nullable=True)
    success:       Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index("idx_usage_logs_user_id", "user_id"),
        Index("idx_usage_logs_project_id", "project_id"),
        Index("idx_usage_logs_created_at", "created_at"),
        Index("idx_usage_logs_operation", "operation"),
    )


# ── Knowledge Graph Node ─────────────────────────────────────────

class GraphNode(Base):
    """
    A node in the Architecture Knowledge Graph.
    Nodes represent domains, capabilities, entities, modules, components, tables, etc.
    """
    __tablename__ = "graph_nodes"

    id:          Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    node_type:   Mapped[str]      = mapped_column(String(50), nullable=False)               # "domain" | "capability" | "entity" | "rule" | "table" | "column"
    name:        Mapped[str]      = mapped_column(String(255), nullable=False)
    properties:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)                  # Description, metadata, etc.
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index("idx_graph_nodes_type_name", "node_type", "name"),
        UniqueConstraint("node_type", "name", name="uq_node_type_name"),
    )


# ── Knowledge Graph Edge ─────────────────────────────────────────

class GraphEdge(Base):
    """
    A directed edge in the Architecture Knowledge Graph.
    Connects two nodes to represent relationships like "requires", "implements", "contains", etc.
    """
    __tablename__ = "graph_edges"

    id:          Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id:   Mapped[str]      = mapped_column(String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False)
    target_id:   Mapped[str]      = mapped_column(String(36), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False)
    edge_type:   Mapped[str]      = mapped_column(String(50), nullable=False)               # "requires" | "implements" | "contains" | "triggers" | "relates_to"
    properties:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)                  # Weight, count, confidence, etc.
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "edge_type", name="uq_edge_source_target_type"),
        Index("idx_graph_edges_source", "source_id"),
        Index("idx_graph_edges_target", "target_id"),
    )


# ── Competitive Benchmarking & Validation ───────────────────────

class BenchmarkRun(Base):
    """
    Stores the results of a competitive benchmark run comparing BaseAI against generic LLMs.
    """
    __tablename__ = "benchmark_runs"

    id:               Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    requirement_name: Mapped[str]      = mapped_column(String(255), nullable=False)           # e.g., "E-Commerce Ledger"
    provider:         Mapped[str]      = mapped_column(String(50), nullable=False)            # "base_ai" | "claude_3_5" | "gpt_4o"
    overall_score:    Mapped[float]    = mapped_column(Float, nullable=False)
    metrics:          Mapped[dict]     = mapped_column(JSON, nullable=False)                  # Normalization, audit, etc.
    blueprint:        Mapped[dict]     = mapped_column(JSON, nullable=False)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


# ── Customer Intelligence & Continuous Learning ──────────────────

class RecommendationFeedback(Base):
    """
    Logs user actions on AI recommendations (Accepted, Modified, Rejected, Ignored).
    Used to calculate recommendation quality and drive learning proposals.
    """
    __tablename__ = "recommendation_feedback"

    id:                  Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id:          Mapped[str]      = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    recommendation_type: Mapped[str]      = mapped_column(String(50), nullable=False)           # "table" | "relationship" | "component"
    item_name:           Mapped[str]      = mapped_column(String(255), nullable=False)          # e.g., "PatientConsent"
    action:              Mapped[str]      = mapped_column(String(20), nullable=False)           # "accepted" | "modified" | "rejected" | "ignored"
    user_id:             Mapped[str]      = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at:          Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class LearningProposal(Base):
    """
    Generated by the learning engine when patterns emerge from user feedback.
    Must be approved by an administrator before becoming an active rule.
    """
    __tablename__ = "learning_proposals"

    id:               Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    pattern_type:     Mapped[str]      = mapped_column(String(50), nullable=False)           # "table_association" | "naming_convention"
    suggested_rule:   Mapped[dict]     = mapped_column(JSON, nullable=False)                 # The rule definition JSON
    status:           Mapped[str]      = mapped_column(String(20), default="pending", nullable=False) # "pending" | "approved" | "rejected"
    confidence_score: Mapped[float]    = mapped_column(Float, nullable=False)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class OrganizationMemory(Base):
    """
    Maintains an organization's specific architectural style and naming preferences.
    Injected into the L1 system prompt for all projects in the organization.
    """
    __tablename__ = "organization_memories"

    id:                   Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id:               Mapped[str]      = mapped_column(String(100), unique=True, nullable=False)
    naming_style:         Mapped[str]      = mapped_column(String(100), default="standard")      # e.g., "suffix_header_all"
    preferred_components: Mapped[dict]     = mapped_column(JSON, default=dict, nullable=False)   # Preferred packages
    updated_at:           Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
