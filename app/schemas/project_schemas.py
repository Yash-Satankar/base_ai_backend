# app/schemas/project_schemas.py
"""
Pydantic schemas for Projects and ProjectVersions persistence.
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Name of the project/application")
    description: Optional[str] = Field(None, max_length=1000, description="Description of the project scope")
    domain: Optional[str] = Field(None, max_length=100, description="Primary business domain")


class ProjectResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    description: Optional[str] = None
    domain: Optional[str] = None
    version_count: int
    latest_score: Optional[int] = None
    latest_table_count: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class ProjectVersionResponse(BaseModel):
    id: str
    project_id: str
    version_number: int
    status: str
    domain: Optional[str] = None
    scale: Optional[str] = None
    gst_required: bool
    modules_count: Optional[int] = None
    tables_count: Optional[int] = None
    rules_applied_count: Optional[int] = None
    validation_score: Optional[int] = None
    validation_grade: Optional[str] = None
    created_at: datetime


class ProjectDetailResponse(ProjectResponse):
    versions: List[ProjectVersionResponse] = []


class VersionDetailResponse(ProjectVersionResponse):
    blueprint: Optional[dict] = None
    schema_sql: Optional[str] = None
    validation_report: Optional[dict] = None
    sql_file_path: Optional[str] = None
    pdf_file_path: Optional[str] = None
    generation_error: Optional[str] = None
