# app/schemas/planner_schemas.py

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional


# ── Validation models (defined first – used in request models below) ─────────

class ValidationIssueResponse(BaseModel):
    rule_id: int
    rule_name: str
    severity: str
    issue: str
    suggestion: str
    table: Optional[str] = None


class ValidationResponse(BaseModel):
    score: int
    passed: bool
    grade: str
    summary: str
    total_issues: int
    critical_issues: int
    high_issues: int
    medium_issues: int
    scores_breakdown: dict
    tables_found: list[str]
    issues: list[ValidationIssueResponse]


# ── Request models ───────────────────────────────────────────────

class GenerateSchemaRequest(BaseModel):
    requirement: str = Field(
        ...,
        min_length=20,
        max_length=50000,
        description="Describe the database you need",
        example="Build a school fee management system with student records, fee collection, receipts, and payment history"
    )
    additional_context: Optional[str] = Field(
        None,
        max_length=2000,
        description="Any extra context (tech stack, scale, special requirements)",
        example="This is for an Indian school. Must support GST invoicing."
    )
    blueprint: Optional[dict] = Field(
        None,
        description="Optional pre-generated blueprint to guide schema generation"
    )
    session_id: Optional[str] = Field(
        None,
        description="Optional conversation session ID to link updates back to the chat"
    )


class MatchRulesRequest(BaseModel):
    requirement: str = Field(
        ...,
        min_length=10,
        max_length=50000,
        description="Requirement text to match rules against",
    )


class SearchRulesRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="Search query to find relevant rules",
        example="approval workflow with multiple levels"
    )
    top_k: Optional[int] = Field(
        10,
        ge=1,
        le=50,
        description="Number of rules to return"
    )
    category: Optional[str] = Field(
        None,
        description="Filter by category: naming, financial, workflow, etc."
    )


# ── Response models ──────────────────────────────────────────────

class RuleSummary(BaseModel):
    rule_id: int
    rule_name: str
    priority: str
    category: str


class RuleDetail(BaseModel):
    rule_id: int
    rule_name: str
    priority: str
    category: str
    trigger_when: list[str]
    enforce: list[str]
    avoid: Optional[list[str]] = []
    reason: Optional[str] = None
    tags: Optional[list[str]] = []
    relevance_score: Optional[float] = None


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class GenerationMetadata(BaseModel):
    primary_domain: str
    all_domains: list[str]
    domain_confidence: float
    rules_applied: list[RuleSummary]
    total_rules_applied: int
    semantic_matches: int
    ai_provider: str
    ai_model: str
    token_usage: TokenUsage
    generation_time_seconds: float


class GenerationSummary(BaseModel):
    modules_planned:       int
    modules_succeeded:     int
    modules_failed:        int
    failed_module_details: list[dict] = []
    tables_planned:        int
    tables_generated:      int
    completeness_pct:      float
    is_complete:           bool


class GenerateSchemaResponse(BaseModel):
    success:            bool
    schema_sql:         str = Field(..., alias="schema")
    metadata:           GenerationMetadata
    generation_summary: Optional[GenerationSummary] = None
    validation:         Optional[ValidationResponse] = None

    model_config = ConfigDict(populate_by_name=True)


# ── Job (async polling) schemas ──────────────────────────────────

class JobProgressInfo(BaseModel):
    phase:          str              # queued | generating | done | failed
    current_module: Optional[str]
    modules_done:   int
    modules_total:  int
    tables_done:    int
    tables_planned: int


class SubmitJobResponse(BaseModel):
    """Returned immediately when POST /generate is called."""
    success:    bool
    job_id:     str
    status:     str                  # always "queued" at submit time
    poll_url:   str                  # convenience URL for the client


class JobStatusResponse(BaseModel):
    """Returned by GET /job/{job_id}."""
    success:      bool
    job_id:       str
    status:       str                # queued | generating | done | failed
    progress:     Optional[JobProgressInfo] = None
    result:       Optional[dict]     = None   # populated when status == "done"
    error:        Optional[str]      = None   # populated when status == "failed"
    created_at:   Optional[str]      = None
    started_at:   Optional[str]      = None
    completed_at: Optional[str]      = None


class MatchRulesResponse(BaseModel):
    success: bool
    primary_domain: str
    all_domains: list[str]
    domain_confidence: float
    total_rules: int
    semantic_matches: int
    rules: list[dict]


class SearchRulesResponse(BaseModel):
    success: bool
    query: str
    total_results: int
    rules: list[RuleDetail]


# ── Blueprint models ─────────────────────────────────────────────

class GenerateBlueprintRequest(BaseModel):
    requirement: str = Field(
        ...,
        min_length=20,
        max_length=50000,
        description="Describe the database requirement for the blueprint",
        example="Build a school fee management system with student records, fee collection, receipts, and payment history"
    )
    domain: Optional[str] = Field(
        None,
        description="Optional domain, automatically detected if not specified"
    )
    gst_required: Optional[bool] = Field(
        None,
        description="Optional GST requirement, automatically detected if not specified"
    )
    scale: Optional[str] = Field(
        "medium",
        description="Scale of the system (small, medium, large)"
    )


class BlueprintTable(BaseModel):
    name: str
    purpose: str


class BlueprintModule(BaseModel):
    name: str
    description: str
    tables: list[BlueprintTable]


class GenerateBlueprintResponse(BaseModel):
    success: bool
    project_name: str
    description: str
    domain: str
    gst_required: bool
    scale: str
    modules: list[BlueprintModule]
