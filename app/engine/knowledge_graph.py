# app/engine/knowledge_graph.py
"""
Knowledge Graph Engine: Manages the node/edge insertions, relationship mapping,
and graph telemetry queries. Builds compounding architectural intelligence.
"""

import logging
from typing import List, Dict, Tuple, Any
from sqlalchemy import select, update, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import GraphNode, GraphEdge

logger = logging.getLogger(__name__)


# ── Graph Writers ────────────────────────────────────────────────────────────

async def upsert_node(db: AsyncSession, node_type: str, name: str, properties: dict = None) -> str:
    """Upsert a graph node (insert or update properties). Returns node ID."""
    stmt = pg_insert(GraphNode).values(
        node_type=node_type,
        name=name,
        properties=properties or {}
    )
    
    # On conflict, merge properties
    stmt = stmt.on_conflict_do_update(
        constraint="uq_node_type_name",
        set_={
            "properties": GraphNode.properties + (properties or {})
        }
    )
    
    await db.execute(stmt)
    
    # Retrieve ID
    query = await db.execute(
        select(GraphNode.id).where(GraphNode.node_type == node_type, GraphNode.name == name)
    )
    return query.scalar_one()


async def upsert_edge(db: AsyncSession, source_id: str, target_id: str, edge_type: str, properties: dict = None) -> None:
    """Upsert a graph edge. Increments a 'weight' counter in properties on conflict."""
    props = properties or {}
    props["weight"] = props.get("weight", 1)

    # We use a select-then-upsert style because on_conflict_do_update is tricky with JSONB addition
    existing_query = await db.execute(
        select(GraphEdge)
        .where(
            GraphEdge.source_id == source_id,
            GraphEdge.target_id == target_id,
            GraphEdge.edge_type == edge_type
        )
    )
    existing = existing_query.scalar_one_or_none()

    if existing:
        current_props = existing.properties or {}
        current_props["weight"] = current_props.get("weight", 1) + 1
        existing.properties = current_props
    else:
        new_edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            properties=props
        )
        db.add(new_edge)


async def save_project_to_graph(
    db: AsyncSession,
    l1: dict,
    l2: dict,
    l3: dict,
    l4: dict,
    l5: dict,
    l7: dict,
    rules: list,
) -> None:
    """
    Parses the intermediate L1-L7 specifications of a project,
    builds the nodes and edges, and commits them to the PostgreSQL Knowledge Graph.
    """
    try:
        logger.info(f"🕸️  Indexing project '{l1.get('project_name')}' into the Architecture Knowledge Graph...")

        # 1. Domain Node (L1)
        domain_name = l1.get("domain", "general")
        domain_id = await upsert_node(db, "domain", domain_name, {
            "description": f"Primary domain: {domain_name}",
            "scale": l1.get("scale", "medium")
        })

        # 2. Capabilities (L2)
        cap_ids = {}
        for cap in l2.get("capabilities", []):
            cap_id = await upsert_node(db, "capability", cap["name"], {
                "description": cap["description"],
                "reusable_component_id": cap.get("reusable_component_id")
            })
            cap_ids[cap["name"]] = cap_id
            # Domain --(requires)--> Capability
            await upsert_edge(db, domain_id, cap_id, "requires")

        # 3. Workflows (L3)
        wf_ids = {}
        for wf in l3.get("workflows", []):
            wf_id = await upsert_node(db, "workflow", wf["name"], {
                "description": wf["description"]
            })
            wf_ids[wf["name"]] = wf_id
            
            # Map workflow to capability (heuristically by name/description)
            # Link to all capabilities for simplicity, or match keywords
            for cap_name, cap_id in cap_ids.items():
                if cap_name.lower() in wf["description"].lower() or cap_name.lower() in wf["name"].lower():
                    await upsert_edge(db, cap_id, wf_id, "implements")

        # 4. Entities (L4)
        ent_ids = {}
        for ent in l4.get("entities", []):
            ent_id = await upsert_node(db, "entity", ent["name"], {
                "description": ent["description"],
                "entity_type": ent["entity_type"],
                "component_owner": ent.get("component_owner")
            })
            ent_ids[ent["name"]] = ent_id
            
            # Link workflows affecting this entity
            for wf in l3.get("workflows", []):
                for step in wf.get("steps", []):
                    if step["entity_affected"].lower() == ent["name"].lower():
                        wf_id = wf_ids.get(wf["name"])
                        if wf_id:
                            await upsert_edge(db, wf_id, ent_id, "affects")

        # 5. Entity Relationships (L5)
        for rel in l5.get("relationships", []):
            src_id = ent_ids.get(rel["source_entity"])
            tgt_id = ent_ids.get(rel["target_entity"])
            if src_id and tgt_id:
                await upsert_edge(db, src_id, tgt_id, "relates_to", {
                    "cardinality": rel["relationship_type"],
                    "description": rel["description"]
                })

        # 6. Rules Applied
        for r in rules:
            rule_id = await upsert_node(db, "rule", f"Rule_{r.get('rule_id')}", {
                "rule_name": r.get("rule_name"),
                "priority": r.get("priority")
            })
            # Link Domain to Rules triggered
            await upsert_edge(db, domain_id, rule_id, "triggers")

        await db.flush()
        logger.info("🕸️  Knowledge Graph indexing completed successfully.")

    except Exception as e:
        logger.error(f"❌ Failed to index project to Knowledge Graph: {e}", exc_info=True)
        # We do not raise here to prevent failing the entire generation job
        # if only the graph indexing fails.


# ── Graph Telemetry Queries (for Platform Intelligence Dashboard) ──────────────

async def get_graph_stats(db: AsyncSession) -> dict:
    """Returns counts of nodes and edges in the graph."""
    node_count_q = await db.execute(select(func.count(GraphNode.id)))
    edge_count_q = await db.execute(select(func.count(GraphEdge.id)))
    
    node_types_q = await db.execute(
        select(GraphNode.node_type, func.count(GraphNode.id))
        .group_by(GraphNode.node_type)
    )
    
    return {
        "total_nodes": node_count_q.scalar(),
        "total_edges": edge_count_q.scalar(),
        "node_types": {row[0]: row[1] for row in node_types_q.all()}
    }

from sqlalchemy import func
