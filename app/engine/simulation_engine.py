# app/engine/simulation_engine.py
"""
Simulation Engine: Deterministically simulates physical database performance,
calculating write amplification, join depth, and bottleneck indices.
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def simulate_architecture(blueprint_json: dict, relationships_json: dict, scale: str) -> dict:
    """
    Deterministically simulates database performance characteristics.
    Returns a structured Architecture Health Report.
    """
    logger.info("⚡ Simulating database architecture performance...")

    scale_multiplier = {
        "small": 1.0,
        "medium": 5.0,
        "large": 50.0,
        "enterprise": 500.0
    }.get(scale.lower(), 5.0)

    modules = blueprint_json.get("modules", [])
    relationships = relationships_json.get("relationships", [])

    # 1. Build adjacency list for relationship path analysis
    adj_list = {}
    for rel in relationships:
        src = rel.get("source_entity")
        tgt = rel.get("target_entity")
        if src and tgt:
            adj_list.setdefault(src, []).append(tgt)

    # 2. Calculate Write Amplification and Join Depth per table
    table_simulations = {}
    total_write_amp = 0.0
    table_count = 0

    for module in modules:
        for table in module.get("tables", []):
            table_name = table["name"]
            
            # Write Amplification Calculation
            write_amp = 1.0  # Base write
            reasons = ["Base Insert"]

            if table.get("requires_archive"):
                write_amp += 1.0
                reasons.append("Archive Mirroring (_archive_all)")
            if table.get("requires_lifecycle"):
                write_amp += 1.0
                reasons.append("State Lifecycle Tracking (_life_cycle_all)")
            
            # Every write to a transactional/header table triggers a central audit log entry
            if table.get("table_type") in ["HEADER", "TRANSACTION"]:
                write_amp += 1.0
                reasons.append("Central Audit Log Entry")

            # Calculate Join Depth (longest path in relationship graph)
            entity_name = table.get("entity_name", "")
            join_depth = _calculate_max_depth(entity_name, adj_list, set())

            # Archival Cost Index (storage growth projection)
            archival_cost_index = 0.1
            if table.get("requires_archive"):
                archival_cost_index = round(0.5 * scale_multiplier, 2)

            table_simulations[table_name] = {
                "write_amplification": write_amp,
                "write_amplification_reasons": reasons,
                "join_depth": join_depth,
                "archival_cost_index": archival_cost_index,
                "estimated_monthly_rows_million": round(0.1 * scale_multiplier, 3),
            }

            total_write_amp += write_amp
            table_count += 1

    avg_write_amp = round(total_write_amp / table_count, 2) if table_count > 0 else 1.0

    # 3. Identify likely bottlenecks
    bottlenecks = []
    for t_name, sim in table_simulations.items():
        if sim["write_amplification"] >= 4.0:
            bottlenecks.append({
                "table": t_name,
                "type": "High Write Amplification",
                "score": sim["write_amplification"],
                "recommendation": f"Partition '{t_name}' or throttle audits to avoid transaction log bottlenecks."
            })
        if sim["join_depth"] >= 3:
            bottlenecks.append({
                "table": t_name,
                "type": "Deep Join Path",
                "score": sim["join_depth"],
                "recommendation": f"Add composite indexes on foreign keys to accelerate join depth of {sim['join_depth']}."
            })

    # 4. Score overall architecture health
    health_score = 100
    health_score -= len(bottlenecks) * 5
    health_score = max(50, health_score)

    return {
        "health_score": health_score,
        "average_write_amplification": avg_write_amp,
        "scale_simulated": scale,
        "bottlenecks": bottlenecks,
        "table_simulations": table_simulations
    }


def _calculate_max_depth(node: str, adj_list: dict, visited: set) -> int:
    """DFS helper to find the longest relationship chain path."""
    if node not in adj_list or node in visited:
        return 0
    
    visited.add(node)
    max_depth = 0
    for neighbor in adj_list.get(node, []):
        max_depth = max(max_depth, _calculate_max_depth(neighbor, adj_list, visited))
    visited.remove(node)
    
    return max_depth + 1
