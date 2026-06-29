# app/services/architecture_reviewer.py
"""
AI Architecture Reviewer: Parses external SQL DDL files and runs them through
the Multi-Agent Council and Simulation Engine to produce a comprehensive audit.
"""

import re
import logging
from typing import Dict, List, Any
from app.engine.council import run_architecture_council
from app.engine.simulation_engine import simulate_architecture

logger = logging.getLogger(__name__)


def review_external_sql(sql_content: str, scale: str = "medium") -> dict:
    """
    Parses a raw SQL DDL string, reconstructs a logical blueprint,
    and runs it through the Architecture Council and Simulation Engine.
    """
    logger.info("🔍 Reviewing external SQL schema...")

    # 1. Parse SQL into a mock blueprint
    blueprint = _parse_sql_to_blueprint(sql_content)
    
    # 2. Extract mock L1-L7 objects for the Council (derived from tables)
    l1 = {
        "project_name": "Uploaded Schema Review",
        "business_goal": "Auditing legacy database schema",
        "scale": scale,
        "compliance_requirements": ["General Security"]
    }
    
    l2 = {
        "capabilities": [
            {"name": m["name"], "description": m["description"]}
            for m in blueprint["modules"]
        ]
    }
    
    l3 = {"workflows": []}
    l4 = {
        "entities": [
            {"name": t["name"], "description": "Parsed SQL Table", "entity_type": "transactional"}
            for m in blueprint["modules"]
            for t in m["tables"]
        ]
    }
    l5 = {"relationships": []}
    l6 = {"lifecycles": []}
    l7 = {
        "modules": [
            {"name": m["name"], "tables": [t["name"] for t in m["tables"]]}
            for m in blueprint["modules"]
        ]
    }

    # 3. Run Multi-Agent Council Review
    council_synthesis, avg_score = run_architecture_council(
        l1=l1, l2=l2, l3=l3, l4=l4, l5=l5, l6=l6, l7=l7, blueprint=blueprint
    )

    # 4. Run Performance Simulation
    simulation_report = simulate_architecture(
        blueprint_json=blueprint,
        relationships_json=l5,
        scale=scale
    )

    return {
        "success": True,
        "score": avg_score,
        "health_score": simulation_report["health_score"],
        "average_write_amplification": simulation_report["average_write_amplification"],
        "bottlenecks": simulation_report["bottlenecks"],
        "council_verdict": council_synthesis.get("consensus_verdict"),
        "council_findings": [
            f"{agent}: {', '.join(rev.get('findings', []))}"
            for agent, rev in council_synthesis.get("individual_reviews", {}).items()
        ],
        "parsed_tables_count": sum(len(m["tables"]) for m in blueprint["modules"]),
    }


def _parse_sql_to_blueprint(sql: str) -> dict:
    """Helper to parse SQL tables and columns using regex."""
    # Split into individual CREATE TABLE blocks
    table_blocks = re.findall(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`"]?\w+[`"]?)\s*\((.*?)\);',
        sql,
        re.DOTALL | re.IGNORECASE
    )

    tables = []
    for raw_name, body in table_blocks:
        table_name = raw_name.replace('`', '').replace('"', '').strip()
        
        # Parse columns inside body
        col_matches = re.findall(
            r'^\s*([`"]?\w+[`"]?)\s+(\w+(?:\([\d,\s]+\))?)',
            body,
            re.MULTILINE
        )
        
        columns = []
        for c_name, c_type in col_matches:
            col_name = c_name.replace('`', '').replace('"', '').strip()
            # Skip SQL keywords captured by greedy regex
            if col_name.upper() in ["PRIMARY", "KEY", "FOREIGN", "CONSTRAINT", "UNIQUE", "INDEX"]:
                continue
            columns.append({
                "name": col_name,
                "type": c_type.upper()
            })

        tables.append({
            "name": table_name,
            "table_type": "HEADER" if "history" not in table_name else "ARCHIVE",
            "columns": columns,
            "requires_archive": "history" in table_name,
            "requires_lifecycle": "status" in [c["name"] for c in columns]
        })

    # Group into a single default module for review
    return {
        "project_name": "Imported Schema",
        "domain": "general",
        "scale": "medium",
        "gst_required": False,
        "modules": [
            {
                "name": "Imported Core",
                "description": "Auto-grouped tables from SQL file",
                "tables": tables
            }
        ]
    }
