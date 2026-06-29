# app/engine/genome.py
"""
Genome Engine: Calculates the structural DNA (genome) of a database architecture
and benchmarks it against historical reference architectures.
"""

import logging
import math
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def calculate_genome(
    l1: dict,
    l2: dict,
    l3: dict,
    l4: dict,
    l5: dict,
    l6: dict,
    blueprint: dict
) -> dict:
    """
    Computes a normalized DNA vector (0.0 to 1.0) representing the structural
    and functional characteristics of the database architecture.
    """
    logger.info("🧬 Calculating Architecture Genome...")

    # 1. Workflow Complexity (Workflows / Entities ratio)
    workflow_count = len(l3.get("workflows", []))
    entity_count = len(l4.get("entities", []))
    workflow_complexity = min(1.0, round(workflow_count / max(1, entity_count), 2))

    # 2. Audit Intensity (Archive & Lifecycle tables / Header tables ratio)
    tables = [t for m in blueprint.get("modules", []) for t in m.get("tables", [])]
    header_tables = [t for t in tables if t.get("table_type") == "HEADER"]
    archive_tables = [t for t in tables if t.get("table_type") == "ARCHIVE"]
    lifecycle_tables = [t for t in tables if t.get("table_type") == "LIFECYCLE"]
    
    audit_intensity = min(1.0, round((len(archive_tables) + len(lifecycle_tables)) / max(1, len(header_tables) * 2), 2))

    # 3. Financial Depth (Presence of LedgerEngine and financial keywords)
    has_ledger = any(c.get("reusable_component_id") == "LedgerEngine" for c in l2.get("capabilities", []))
    financial_depth = 1.0 if has_ledger else 0.1

    # 4. Document Density (Presence of attachment or document entities)
    doc_keywords = ["document", "attachment", "file", "image", "media", "contract"]
    doc_entities = [e for e in l4.get("entities", []) if any(kw in e["name"].lower() for kw in doc_keywords)]
    document_density = min(1.0, round(len(doc_entities) / max(1, entity_count), 2))

    # 5. Approval Complexity
    has_approval = any(c.get("reusable_component_id") == "ApprovalEngine" for c in l2.get("capabilities", []))
    approval_complexity = 1.0 if has_approval else 0.1

    # 6. Lifecycle Depth
    lifecycle_count = len(l6.get("lifecycles", []))
    lifecycle_depth = min(1.0, round(lifecycle_count / max(1, entity_count), 2))

    # 7. Reuse Score (Percentage of tables owned by reusable components)
    reusable_tables = [t for t in tables if t.get("is_reusable_component_table")]
    reuse_score = min(1.0, round(len(reusable_tables) / max(1, len(tables)), 2))

    # Determine Compliance Level
    compliance_reqs = l1.get("compliance_requirements", [])
    compliance_level = "None"
    if "HIPAA" in compliance_reqs:
        compliance_level = "HIPAA"
    elif "PCI-DSS" in compliance_reqs:
        compliance_level = "PCI-DSS"
    elif "GDPR" in compliance_reqs:
        compliance_level = "GDPR"

    return {
        "workflow_complexity": workflow_complexity,
        "audit_intensity": audit_intensity,
        "financial_depth": financial_depth,
        "document_density": document_density,
        "approval_complexity": approval_complexity,
        "lifecycle_depth": lifecycle_depth,
        "compliance_level": compliance_level,
        "reuse_score": reuse_score
    }


def benchmark_project(genome: dict, historical_genomes: List[dict]) -> dict:
    """
    Benchmarks the current project genome against a list of historical genomes
    using Euclidean distance. Returns similarity matches.
    """
    if not historical_genomes:
        # Default fallback reference benchmarks
        historical_genomes = [
            {
                "name": "Enterprise ERP Reference Architecture",
                "workflow_complexity": 0.8,
                "audit_intensity": 0.9,
                "financial_depth": 0.7,
                "document_density": 0.5,
                "approval_complexity": 0.8,
                "lifecycle_depth": 0.8,
                "reuse_score": 0.6
            },
            {
                "name": "SaaS Multi-Tenant Boilerplate",
                "workflow_complexity": 0.4,
                "audit_intensity": 0.3,
                "financial_depth": 0.2,
                "document_density": 0.3,
                "approval_complexity": 0.2,
                "lifecycle_depth": 0.3,
                "reuse_score": 0.8
            }
        ]

    benchmarks = []
    keys = ["workflow_complexity", "audit_intensity", "financial_depth", "document_density", "approval_complexity", "lifecycle_depth", "reuse_score"]

    for h_gen in historical_genomes:
        # Calculate Euclidean distance
        distance = 0.0
        for k in keys:
            distance += (genome.get(k, 0.5) - h_gen.get(k, 0.5)) ** 2
        distance = math.sqrt(distance)
        
        # Convert distance to similarity percentage
        max_dist = math.sqrt(len(keys))  # Max possible distance in unit hypercube
        similarity = round((1.0 - (distance / max_dist)) * 100, 1)

        benchmarks.append({
            "reference_name": h_gen.get("name") or f"Historical Project ({h_gen.get('domain', 'general')})",
            "similarity_percentage": similarity
        })

    # Sort by similarity descending
    benchmarks = sorted(benchmarks, key=lambda x: x["similarity_percentage"], reverse=True)

    return {
        "benchmarks": benchmarks,
        "closest_match": benchmarks[0] if benchmarks else None
    }
