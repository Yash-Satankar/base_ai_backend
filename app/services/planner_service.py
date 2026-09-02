# app/services/planner_service.py

import re
import time
import logging
import asyncio
from app.engine.rule_matcher import match_rules
from app.prompts.system_prompt import (
    build_system_prompt,
    build_module_prompt,
    build_stitch_prompt,
)
from app.services.ai_service import generate_schema
from app.validators.schema_validator import SchemaValidator
from app.core.config import settings
from app.services.file_service import generate_sql_file, generate_pdf_documentation
from app.db.session_store import save_session, load_session
from app.engine.conversation_engine import ConversationStage
from app.services.planner_helpers import (
    batch_tables,
    stitch_modules,
    clean_sql,
    run_fix_pass
)

logger = logging.getLogger(__name__)

# ── Key tuning constant ───────────────────────────────────────────
# Each AI call must stay comfortably within the model's output limit.
# llama-3.3-70b caps at 8192 output tokens ≈ 4 deep tables.
# Keeping this at 4 prevents truncation mid-CREATE-TABLE.
MAX_TABLES_PER_BATCH = 4


def generate_database_schema(
    requirement: str,
    blueprint: dict = None,
    additional_context: str = None,
    session_id: str = None,
) -> dict:
    """
    Multi-pass database schema generation.
    Generates tables in small batches (MAX_TABLES_PER_BATCH per AI call),
    stitches all batches together, validates, and auto-fixes.
    Target: 80-120 tables with full depth.
    """
    from app.db.session_store import load_session
    start_time = time.time()

    # ── Step 1: Match rules ──────────────────────────────────────
    match_result = match_rules(requirement)
    rules = match_result["rules"]
    primary_domain = match_result["primary_domain"]

    system_prompt = build_system_prompt(rules)

    # ── Step 2: Get modules from blueprint (L1 -> L8 compilation) ──
    l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data = None, None, None, None, None, None, None
    council_synthesis, simulation_report, genome, benchmarks, recommendations = None, None, None, None, None
    if blueprint and blueprint.get("modules"):
        modules = blueprint["modules"]
        gst_required = blueprint.get("gst_required", False)
        scale = blueprint.get("scale", "medium")
        project_name = blueprint.get("project_name", "Project")

        # Load L1-L7 metadata from session if available
        if session_id:
            state = load_session(session_id)
            if state:
                l1_data = state.l1_data
                l2_data = state.l2_data
                l3_data = state.l3_data
                l4_data = state.l4_data
                l5_data = state.l5_data
                l6_data = state.l6_data
                l7_data = state.l7_data

                if l1_data:
                    from app.engine.council import run_architecture_council
                    from app.engine.simulation_engine import simulate_architecture
                    from app.engine.genome import calculate_genome, benchmark_project
                    from app.engine.recommendation_engine import generate_recommendations

                    council_synthesis, _ = run_architecture_council(
                        l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data, blueprint
                    )
                    simulation_report = simulate_architecture(blueprint, l5_data, scale)
                    genome = calculate_genome(
                        l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, blueprint
                    )
                    benchmarks = benchmark_project(genome, [])
                    try:
                        recommendations = asyncio.run(generate_recommendations(blueprint, None))
                    except Exception:
                        recommendations = []
    else:
        from app.engine.abstraction_pipeline import (
            generate_l1_understanding,
            compile_l1_to_l2,
            compile_l2_to_l3,
            compile_l3_to_l4,
            compile_l4_to_l5_l6_l7,
            compile_to_l8_blueprint,
        )
        l1 = generate_l1_understanding(requirement)
        l2 = compile_l1_to_l2(l1)
        l3 = compile_l2_to_l3(l1, l2)
        l4 = compile_l3_to_l4(l1, l3)
        l5, l6, l7 = compile_l4_to_l5_l6_l7(l1, l4)
        bp_spec = compile_to_l8_blueprint(l1, l4, l5, l6, l7)

        modules = [m.model_dump() for m in bp_spec.modules]
        gst_required = bp_spec.gst_required
        scale = bp_spec.scale
        project_name = bp_spec.project_name

        l1_data = l1.model_dump()
        l2_data = l2.model_dump()
        l3_data = l3.model_dump()
        l4_data = l4.model_dump()
        l5_data = l5.model_dump()
        l6_data = l6.model_dump()
        l7_data = l7.model_dump()

        # Run Council, Simulation, and Genome calculations
        from app.engine.council import run_architecture_council
        from app.engine.simulation_engine import simulate_architecture
        from app.engine.genome import calculate_genome, benchmark_project
        from app.engine.recommendation_engine import generate_recommendations

        council_synthesis, _ = run_architecture_council(
            l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data, bp_spec.model_dump()
        )
        simulation_report = simulate_architecture(bp_spec.model_dump(), l5_data, scale)
        genome = calculate_genome(
            l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, bp_spec.model_dump()
        )
        benchmarks = benchmark_project(genome, [])
        try:
            recommendations = asyncio.run(generate_recommendations(bp_spec.model_dump(), None))
        except Exception:
            recommendations = []

    tables_planned = sum(len(m.get("tables", [])) for m in modules)
    logger.info(
        f"📋 Blueprint: {len(modules)} modules, "
        f"{tables_planned} tables planned, "
        f"batched at {MAX_TABLES_PER_BATCH} tables/call"
    )

    # ── Step 3: Generate each module in small batches ────────────
    all_sql_parts: list[dict] = []
    generated_tables: list[str] = []
    failed_modules: list[dict] = []
    total_input_tokens = 0
    total_output_tokens = 0

    for i, module in enumerate(modules):
        module_tables = module.get("tables", [])
        batches = batch_tables(module_tables, MAX_TABLES_PER_BATCH)

        logger.info(
            f"  Module {i+1}/{len(modules)}: '{module['name']}' "
            f"— {len(module_tables)} tables → {len(batches)} batch(es)"
        )

        # Check for reusable component guidelines
        component_guidelines = []
        from app.engine.component_registry import REUSABLE_COMPONENTS
        for table in module_tables:
            for comp_id, comp_spec in REUSABLE_COMPONENTS.items():
                comp_table_names = {t.name for t in comp_spec["tables"]}
                if table.get("name") in comp_table_names:
                    component_guidelines.append(comp_spec["sql_guideline"])
                    break
        extra_guidelines = "\n".join(set(component_guidelines))

        module_sql_parts: list[str] = []
        module_new_tables: list[str] = []
        module_failed_batches: list[int] = []

        for b_idx, batch in enumerate(batches):
            # Build a sub-module dict with only this batch's tables
            batch_module = {
                "name": f"{module['name']} (batch {b_idx + 1}/{len(batches)})",
                "description": module.get("description", ""),
                "tables": batch,
            }
            module_prompt = build_module_prompt(
                module=batch_module,
                domain=primary_domain,
                gst_required=gst_required,
                scale=scale,
                existing_tables=generated_tables,
            )
            if extra_guidelines:
                module_prompt += f"\n\nREUSABLE COMPONENT SQL SPECIFICATIONS:\n{extra_guidelines}"

            try:
                response = generate_schema(
                    system_prompt=system_prompt,
                    user_prompt=module_prompt,
                    max_tokens=2000,
                )
                batch_sql = response["content"]

                # Accumulate real token usage
                usage = response.get("usage", {})
                total_input_tokens  += usage.get("input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)

                # Track newly generated table names
                new_tables = re.findall(
                    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
                    batch_sql, re.IGNORECASE
                )
                generated_tables.extend(new_tables)
                module_new_tables.extend(new_tables)
                module_sql_parts.append(clean_sql(batch_sql))

                logger.info(
                    f"    ✅ Batch {b_idx+1}/{len(batches)}: "
                    f"{len(new_tables)} tables | "
                    f"{usage.get('input_tokens', 0)}→{usage.get('output_tokens', 0)} tokens"
                )

            except Exception as e:
                logger.error(
                    f"    ❌ Module '{module['name']}' batch {b_idx+1} failed: {e}"
                )
                module_failed_batches.append(b_idx + 1)

        if module_sql_parts:
            all_sql_parts.append({
                "module": module["name"],
                "sql": "\n\n".join(module_sql_parts),
                "tables": module_new_tables,
                "batches_succeeded": len(batches) - len(module_failed_batches),
                "batches_total": len(batches),
            })

        if module_failed_batches:
            failed_modules.append({
                "module": module["name"],
                "failed_batches": module_failed_batches,
                "total_batches": len(batches),
            })

    # ── Step 4: Stitch all modules ───────────────────────────────
    logger.info("🧵 Stitching modules together...")
    combined_sql = stitch_modules(all_sql_parts, project_name)

    # ── Step 5: Validate combined schema ─────────────────────────
    validator = SchemaValidator()
    validation = validator.validate(combined_sql)
    total_tables = len(validation.tables_found)

    logger.info(
        f"📊 Final schema: {total_tables} tables | "
        f"Score: {validation.score}/100 | "
        f"Tokens used: {total_input_tokens}→{total_output_tokens}"
    )

    # ── Step 6: Auto-fix critical/high issues ────────────────────
    if validation.score < 80 and validation.issues:
        logger.info("🔧 Running targeted auto-fix pass...")
        combined_sql, validation = run_fix_pass(
            combined_sql, validation, system_prompt
        )

    elapsed = round(time.time() - start_time, 2)

    # ── Build generation summary ──────────────────────────────────
    tables_generated = total_tables
    completeness_pct = round(
        (tables_generated / tables_planned * 100) if tables_planned > 0 else 0, 1
    )

    generation_summary = {
        "modules_planned":   len(modules),
        "modules_succeeded": len(all_sql_parts),
        "modules_failed":    len(failed_modules),
        "failed_module_details": failed_modules,
        "tables_planned":    tables_planned,
        "tables_generated":  tables_generated,
        "completeness_pct":  completeness_pct,
        "is_complete":       len(failed_modules) == 0,
    }

    if failed_modules:
        failed_names = ", ".join(f['module'] for f in failed_modules)
        raise RuntimeError(f"Generation failed for modules: {failed_names}")

    # ── Step 7: Generate Traceability Graph ──────────────────
    traceability_graph = {}
    if l1_data:
        from app.engine.explainability_engine import generate_traceability_graph
        traceability_graph = generate_traceability_graph(
            sql=combined_sql,
            blueprint_json=blueprint or {},
            l1_json=l1_data,
            l2_json=l2_data,
            l3_json=l3_data,
            l4_json=l4_data,
            rules_applied=[
                {
                    "rule_id":   r["rule_id"],
                    "rule_name": r["rule_name"],
                }
                for r in rules
            ]
        )

    return {
        "schema": combined_sql,
        "metadata": {
            "primary_domain":        primary_domain,
            "all_domains":           match_result["all_domains"],
            "domain_confidence":     match_result["domain_confidence"],
            "rules_applied": [
                {
                    "rule_id":   r["rule_id"],
                    "rule_name": r["rule_name"],
                    "priority":  r["priority"],
                    "category":  r["category"],
                }
                for r in rules
            ],
            "total_rules_applied": len(rules),
            "semantic_matches":    match_result["semantic_matches"],
            "ai_provider":         settings.AI_PROVIDER,
            "ai_model":            settings.GROQ_MODEL,
            "token_usage": {
                "input_tokens":  total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens":  total_input_tokens + total_output_tokens,
            },
            "generation_time_seconds": elapsed,
            "modules_generated":       len(all_sql_parts),
            "tables_per_module": [
                {"module": p["module"], "count": len(p["tables"])}
                for p in all_sql_parts
            ],
            "l1_understanding": l1_data,
            "l2_capabilities":  l2_data,
            "l3_workflows":     l3_data,
            "l4_entities":      l4_data,
            "l5_relationships": l5_data,
            "l6_lifecycles":    l6_data,
            "l7_modules":       l7_data,
            "traceability_graph": traceability_graph,
            "council_synthesis": council_synthesis,
            "simulation_report": simulation_report,
            "genome":            genome,
            "benchmarks":        benchmarks,
            "proactive_recommendations": recommendations,
        },
        "generation_summary": generation_summary,
        "validation": {
            "score":    validation.score,
            "passed":   validation.score >= 80,       # raised from 60 → 80
            "grade": (
                "A" if validation.score >= 90 else
                "B" if validation.score >= 80 else
                "C" if validation.score >= 70 else
                "D" if validation.score >= 60 else "F"
            ),
            "summary":         validation.summary,
            "total_issues":    validation.total_issues,
            "critical_issues": validation.critical_issues,
            "high_issues":     validation.high_issues,
            "medium_issues":   validation.medium_issues,
            "scores_breakdown": validation.scores_breakdown,
            "tables_found":     validation.tables_found,
            "issues": [
                {
                    "rule_id":    i.rule_id,
                    "rule_name":  i.rule_name,
                    "severity":   i.severity,
                    "issue":      i.issue,
                    "suggestion": i.suggestion,
                    "table":      i.table_name,
                }
                for i in validation.issues
            ],
        },
    }


def get_matched_rules_only(requirement: str) -> dict:
    match_result = match_rules(requirement)
    return {
        "primary_domain":    match_result["primary_domain"],
        "all_domains":       match_result["all_domains"],
        "domain_confidence": match_result["domain_confidence"],
        "total_rules":       match_result["total_rules"],
        "semantic_matches":  match_result["semantic_matches"],
        "rules": [
            {
                "rule_id":      r["rule_id"],
                "rule_name":    r["rule_name"],
                "priority":     r["priority"],
                "category":     r["category"],
                "trigger_when": r.get("trigger_when", [])[:2],
            }
            for r in match_result["rules"]
        ],
    }

async def _persist_version_in_db(version_id: str, schema_sql: str, sql_file_path: str, pdf_file_path: str, validation_data: dict, metadata: dict) -> None:
    """Async database operations for updating a completed schema generation run in PostgreSQL."""
    from app.db.database import AsyncSessionLocal
    from app.db.repositories.project_repo import ProjectRepository

    async with AsyncSessionLocal() as db:
        try:
            repo = ProjectRepository(db)
            await repo.complete_version(
                version_id=version_id,
                schema_sql=schema_sql,
                sql_file_path=sql_file_path,
                pdf_file_path=pdf_file_path,
                validation=validation_data,
                metadata=metadata
            )
            await db.commit()
            logger.info(f"✅ Version {version_id} successfully persisted in PostgreSQL database")
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ Failed to persist version {version_id} in PostgreSQL: {e}", exc_info=True)


async def _fail_version_in_db(version_id: str, error_msg: str) -> None:
    """Async database operations for marking a version as failed in PostgreSQL."""
    from app.db.database import AsyncSessionLocal
    from app.db.repositories.project_repo import ProjectRepository
    from app.db.models import VersionStatus

    async with AsyncSessionLocal() as db:
        try:
            repo = ProjectRepository(db)
            await repo.update_version_status(
                version_id=version_id,
                status=VersionStatus.FAILED,
                error=error_msg
            )
            await db.commit()
            logger.info(f"❌ Version {version_id} marked as failed in PostgreSQL database")
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ Failed to mark version {version_id} as failed in PostgreSQL: {e}", exc_info=True)


# ── Async job-aware generation ────────────────────────────────────

def generate_database_schema_for_job(
    job_id: str,
    requirement: str,
    blueprint: dict = None,
    additional_context: str = None,
    session_id: str = None,
    mode: str = "schema",
) -> None:
    """
    Runs the full schema generation pipeline and keeps the job store
    updated with real-time progress.

    Designed to be called via asyncio.to_thread() so it never blocks
    FastAPI's event loop.  Progress updates fire after every module
    batch so the frontend can poll and show a live progress bar.

    ``mode="blueprint"`` diverts to the L1-L8 blueprint compile job
    instead of full SQL generation (Phase 2).
    """
    if mode == "blueprint":
        from app.conversation.blueprint_job import run_blueprint_job
        run_blueprint_job(job_id, session_id, requirement)
        return

    from app.services.job_store import get_job_store
    store = get_job_store()

    try:
        start_time = time.time()

        # ── Step 1: Match rules ──────────────────────────────────
        match_result = match_rules(requirement)
        rules = match_result["rules"]
        primary_domain = match_result["primary_domain"]
        system_prompt = build_system_prompt(rules)

        # ── Step 2: Get modules from blueprint (L1 -> L8 compilation) ──
        l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data = None, None, None, None, None, None, None
        council_synthesis, simulation_report, genome, benchmarks = None, None, None, None
        if blueprint and blueprint.get("modules"):
            modules      = blueprint["modules"]
            gst_required = blueprint.get("gst_required", False)
            scale        = blueprint.get("scale", "medium")
            project_name = blueprint.get("project_name", "Project")

            # Load L1-L7 metadata from session if available
            if session_id:
                state = load_session(session_id)
                if state:
                    l1_data = state.l1_data
                    l2_data = state.l2_data
                    l3_data = state.l3_data
                    l4_data = state.l4_data
                    l5_data = state.l5_data
                    l6_data = state.l6_data
                    l7_data = state.l7_data

                    if l1_data:
                        from app.engine.council import run_architecture_council
                        from app.engine.simulation_engine import simulate_architecture
                        from app.engine.genome import calculate_genome, benchmark_project

                        council_synthesis, _ = run_architecture_council(
                            l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data, blueprint
                        )
                        simulation_report = simulate_architecture(blueprint, l5_data, scale)
                        genome = calculate_genome(
                            l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, blueprint
                        )
                        benchmarks = benchmark_project(genome, [])
        else:
            from app.engine.abstraction_pipeline import (
                generate_l1_understanding,
                compile_l1_to_l2,
                compile_l2_to_l3,
                compile_l3_to_l4,
                compile_l4_to_l5_l6_l7,
                compile_to_l8_blueprint,
            )
            l1 = generate_l1_understanding(requirement)
            l2 = compile_l1_to_l2(l1)
            l3 = compile_l2_to_l3(l1, l2)
            l4 = compile_l3_to_l4(l1, l3)
            l5, l6, l7 = compile_l4_to_l5_l6_l7(l1, l4)
            bp_spec = compile_to_l8_blueprint(l1, l4, l5, l6, l7)

            modules = [m.model_dump() for m in bp_spec.modules]
            gst_required = bp_spec.gst_required
            scale = bp_spec.scale
            project_name = bp_spec.project_name

            l1_data = l1.model_dump()
            l2_data = l2.model_dump()
            l3_data = l3.model_dump()
            l4_data = l4.model_dump()
            l5_data = l5.model_dump()
            l6_data = l6.model_dump()
            l7_data = l7.model_dump()

            # Run Council, Simulation, and Genome calculations
            from app.engine.council import run_architecture_council
            from app.engine.simulation_engine import simulate_architecture
            from app.engine.genome import calculate_genome, benchmark_project

            council_synthesis, _ = run_architecture_council(
                l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, l7_data, bp_spec.model_dump()
            )
            simulation_report = simulate_architecture(bp_spec.model_dump(), l5_data, scale)
            genome = calculate_genome(
                l1_data, l2_data, l3_data, l4_data, l5_data, l6_data, bp_spec.model_dump()
            )
            benchmarks = benchmark_project(genome, [])

        tables_planned = sum(len(m.get("tables", [])) for m in modules)

        store.mark_started(
            job_id,
            modules_total=len(modules),
            tables_planned=tables_planned,
        )
        logger.info(
            f"[job:{job_id[:8]}] Starting: {len(modules)} modules, "
            f"{tables_planned} tables planned"
        )

        # ── Step 3: Generate each module in small batches ────────
        all_sql_parts: list[dict] = []
        generated_tables: list[str] = []
        failed_modules: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0
        tables_done = 0

        for i, module in enumerate(modules):
            module_tables = module.get("tables", [])
            batches = batch_tables(module_tables, MAX_TABLES_PER_BATCH)

            logger.info(
                f"[job:{job_id[:8]}] Module {i+1}/{len(modules)}: "
                f"'{module['name']}' — {len(batches)} batch(es)"
            )

            # Check for reusable component guidelines
            component_guidelines = []
            from app.engine.component_registry import REUSABLE_COMPONENTS
            for table in module_tables:
                for comp_id, comp_spec in REUSABLE_COMPONENTS.items():
                    comp_table_names = {t.name for t in comp_spec["tables"]}
                    if table.get("name") in comp_table_names:
                        component_guidelines.append(comp_spec["sql_guideline"])
                        break
            extra_guidelines = "\n".join(set(component_guidelines))

            module_sql_parts: list[str] = []
            module_new_tables: list[str] = []
            module_failed_batches: list[int] = []

            for b_idx, batch in enumerate(batches):
                batch_module = {
                    "name": f"{module['name']} (batch {b_idx+1}/{len(batches)})",
                    "description": module.get("description", ""),
                    "tables": batch,
                }
                module_prompt = build_module_prompt(
                    module=batch_module,
                    domain=primary_domain,
                    gst_required=gst_required,
                    scale=scale,
                    existing_tables=generated_tables,
                )
                if extra_guidelines:
                    module_prompt += f"\n\nREUSABLE COMPONENT SQL SPECIFICATIONS:\n{extra_guidelines}"

                try:
                    response = generate_schema(
                        system_prompt=system_prompt,
                        user_prompt=module_prompt,
                        max_tokens=2000,
                    )
                    batch_sql = response["content"]
                    usage = response.get("usage", {})
                    total_input_tokens  += usage.get("input_tokens", 0)
                    total_output_tokens += usage.get("output_tokens", 0)

                    new_tables = re.findall(
                        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?',
                        batch_sql, re.IGNORECASE
                    )
                    generated_tables.extend(new_tables)
                    module_new_tables.extend(new_tables)
                    module_sql_parts.append(_clean_sql(batch_sql))
                    tables_done += len(new_tables)

                    logger.info(
                        f"[job:{job_id[:8]}]   Batch {b_idx+1}: "
                        f"{len(new_tables)} tables ({usage.get('output_tokens',0)} out tokens)"
                    )

                except Exception as e:
                    logger.error(
                        f"[job:{job_id[:8]}]   Batch {b_idx+1} failed: {e}"
                    )
                    module_failed_batches.append(b_idx + 1)

                # Report progress after EVERY batch so the frontend sees
                # live updates even within a large module.
                store.update_progress(
                    job_id,
                    current_module=f"{module['name']} (batch {b_idx+1}/{len(batches)})",
                    modules_done=i,        # module not yet done
                    tables_done=tables_done,
                )

                # Brief pause between batches to ease Groq rate limits
                # (reduces 429s that cause long retry back-offs)
                if b_idx < len(batches) - 1:
                    import time as _time
                    _time.sleep(0.5)

            if module_sql_parts:
                all_sql_parts.append({
                    "module":            module["name"],
                    "sql":               "\n\n".join(module_sql_parts),
                    "tables":            module_new_tables,
                    "batches_succeeded": len(batches) - len(module_failed_batches),
                    "batches_total":     len(batches),
                })

            if module_failed_batches:
                failed_modules.append({
                    "module":         module["name"],
                    "failed_batches": module_failed_batches,
                    "total_batches":  len(batches),
                })

            # Final module-level progress update (modules_done = i+1)
            store.update_progress(
                job_id,
                current_module=module["name"],
                modules_done=i + 1,
                tables_done=tables_done,
            )

        # ── Step 4: Stitch & validate ────────────────────────────
        combined_sql = stitch_modules(all_sql_parts, project_name)
        validator    = SchemaValidator()
        validation   = validator.validate(combined_sql)
        total_tables = len(validation.tables_found)

        if validation.score < 80 and validation.issues:
            combined_sql, validation = run_fix_pass(
                combined_sql, validation, system_prompt
            )

        # ── Step 5: Generate Traceability Graph ──────────────────
        traceability_graph = {}
        if l1_data:
            from app.engine.explainability_engine import generate_traceability_graph
            traceability_graph = generate_traceability_graph(
                sql=combined_sql,
                blueprint_json=blueprint or {},
                l1_json=l1_data,
                l2_json=l2_data,
                l3_json=l3_data,
                l4_json=l4_data,
                rules_applied=[
                    {
                        "rule_id":   r["rule_id"],
                        "rule_name": r["rule_name"],
                    }
                    for r in rules
                ]
            )

        elapsed = round(time.time() - start_time, 2)
        tables_generated = total_tables
        completeness_pct = round(
            (tables_generated / tables_planned * 100) if tables_planned > 0 else 0, 1
        )

        # Log structured telemetry
        from app.core.telemetry import TelemetryManager
        TelemetryManager.log_operation(
            operation="generate_database_schema_for_job",
            duration_sec=elapsed,
            tokens={
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens
            },
            model=settings.GROQ_MODEL
        )

        result = {
            "schema": combined_sql,
            "metadata": {
                "primary_domain":          primary_domain,
                "all_domains":             match_result["all_domains"],
                "domain_confidence":       match_result["domain_confidence"],
                "rules_applied": [
                    {
                        "rule_id":   r["rule_id"],
                        "rule_name": r["rule_name"],
                        "priority":  r["priority"],
                        "category":  r["category"],
                    }
                    for r in rules
                ],
                "total_rules_applied":     len(rules),
                "semantic_matches":        match_result["semantic_matches"],
                "ai_provider":             settings.AI_PROVIDER,
                "ai_model":                settings.GROQ_MODEL,
                "token_usage": {
                    "input_tokens":  total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "total_tokens":  total_input_tokens + total_output_tokens,
                },
                "generation_time_seconds": elapsed,
                "modules_generated":       len(all_sql_parts),
                "tables_per_module": [
                    {"module": p["module"], "count": len(p["tables"])}
                    for p in all_sql_parts
                ],
                "l1_understanding": l1_data,
                "l2_capabilities":  l2_data,
                "l3_workflows":     l3_data,
                "l4_entities":      l4_data,
                "l5_relationships": l5_data,
                "l6_lifecycles":    l6_data,
                "l7_modules":       l7_data,
                "traceability_graph": traceability_graph,
                "council_synthesis": council_synthesis,
                "simulation_report": simulation_report,
                "genome":            genome,
                "benchmarks":        benchmarks,
                "proactive_recommendations": [],  # Will be populated by the background save_and_learn task
            },
            "generation_summary": {
                "modules_planned":       len(modules),
                "modules_succeeded":     len(all_sql_parts),
                "modules_failed":        len(failed_modules),
                "failed_module_details": failed_modules,
                "tables_planned":        tables_planned,
                "tables_generated":      tables_generated,
                "completeness_pct":      completeness_pct,
                "is_complete":           len(failed_modules) == 0,
            },
            "validation": {
                "score":    validation.score,
                "passed":   validation.score >= 80,
                "grade": (
                    "A" if validation.score >= 90 else
                    "B" if validation.score >= 80 else
                    "C" if validation.score >= 70 else
                    "D" if validation.score >= 60 else "F"
                ),
                "summary":         validation.summary,
                "total_issues":    validation.total_issues,
                "critical_issues": validation.critical_issues,
                "high_issues":     validation.high_issues,
                "medium_issues":   validation.medium_issues,
                "scores_breakdown": validation.scores_breakdown,
                "tables_found":     validation.tables_found,
                "issues": [
                    {
                        "rule_id":    i.rule_id,
                        "rule_name":  i.rule_name,
                        "severity":   i.severity,
                        "issue":      i.issue,
                        "suggestion": i.suggestion,
                        "table":      i.table_name,
                    }
                    for i in validation.issues
                ],
            },
        }

        store.complete(job_id, result)
        logger.info(
            f"[job:{job_id[:8]}] Done — {tables_generated} tables, "
            f"score {validation.score}/100, {elapsed}s elapsed"
        )

        if session_id:
            state = load_session(session_id)
            if state:
                grade = (
                    "A" if validation.score >= 90 else
                    "B" if validation.score >= 80 else
                    "C" if validation.score >= 70 else
                    "D" if validation.score >= 60 else "F"
                )
                sql_path = generate_sql_file(
                    schema_sql=combined_sql,
                    project_name=project_name,
                    session_id=session_id,
                )
                pdf_path = generate_pdf_documentation(
                    schema_sql=combined_sql,
                    project_name=project_name,
                    session_id=session_id,
                    blueprint=blueprint or {},
                    validation={
                        "score": validation.score,
                        "grade": grade,
                        "tables_found": validation.tables_found,
                        "total_issues": validation.total_issues,
                        "issues": [
                            {
                                "rule_id": i.rule_id,
                                "severity": i.severity,
                                "issue": i.issue,
                                "suggestion": i.suggestion,
                            }
                            for i in validation.issues
                        ],
                    },
                    metadata=result["metadata"],
                    rules_applied=result["metadata"]["rules_applied"],
                )

                state.schema = combined_sql
                state.validation_score = validation.score
                state.sql_file_path = sql_path
                state.pdf_file_path = pdf_path
                state.stage = ConversationStage.COMPLETE

                # Persist to PostgreSQL and run continuous self-improvement in background
                if state.version_id:
                    async def save_and_learn():
                        from app.db.database import AsyncSessionLocal
                        async with AsyncSessionLocal() as db_session:
                            # 1. Generate Recommendations & Index to Knowledge Graph
                            from app.engine.recommendation_engine import generate_recommendations
                            from app.engine.knowledge_graph import save_project_to_graph
                            
                            recs = await generate_recommendations(blueprint or {}, db_session)
                            result["metadata"]["proactive_recommendations"] = recs

                            if l1_data:
                                await save_project_to_graph(
                                    db_session, l1_data, l2_data, l3_data, l4_data, l5_data, l7_data, rules
                                )
                                await db_session.commit()

                            # 2. Complete version persistence
                            from app.db.repositories.project_repo import ProjectRepository
                            repo = ProjectRepository(db_session)
                            await repo.complete_version(
                                version_id=state.version_id,
                                schema_sql=combined_sql,
                                sql_file_path=sql_path,
                                pdf_file_path=pdf_path,
                                validation=result["validation"],
                                metadata=result["metadata"]
                            )
                            await db_session.commit()

                            # 3. Run learning self-improvement loop
                            from app.services.learning_service import run_self_improvement_loop
                            await run_self_improvement_loop(state.version_id, db_session)
                            await db_session.commit()

                            # 4. Phase 3 checkpoint — schema is complete
                            if state.project_id:
                                from app.services import conversation_memory
                                await conversation_memory.persist_checkpoint(
                                    state, db_session, reason="schema_complete", commit=True
                                )

                    try:
                        import asyncio
                        asyncio.run(save_and_learn())
                    except Exception as e:
                        logger.error(f"Failed to persist and learn in background: {e}", exc_info=True)

                incomplete = "\n⚠️ some module(s) had errors — schema is not 100% complete." if len(failed_modules) > 0 else ""
                summary_message = (
                    f"✅ **Schema Generated Successfully!**\n\n"
                    f"**Quality Score: {validation.score}/100 — Grade {grade}**\n"
                    f"**Tables Generated: {tables_generated}**\n"
                    f"**Rules Applied: {len(rules)}**\n"
                    f"**Fix Attempts: {state.fix_attempts}**{incomplete}\n\n"
                    f"Your files are ready:\n"
                    f"📄 **schema.sql** — Run this directly in MySQL\n"
                    f"📋 **documentation.pdf** — Complete logic guide for developers\n\n"
                    f"What would you like to do?\n"
                    f"- Download your files\n"
                    f"- Ask me to explain any table\n"
                    f"- Start a new schema"
                )
                state.add_message("assistant", summary_message)
                save_session(state)
                logger.info(f"[job:{job_id[:8]}] Conversation session {session_id} updated to COMPLETE")

    except Exception as e:
        logger.error(f"[job:{job_id[:8]}] Job failed: {e}", exc_info=True)
        store.fail(job_id, str(e))
        if session_id:
            state = load_session(session_id)
            if state:
                state.stage = ConversationStage.CONFIRMED
                state.add_message("assistant", f"❌ Schema generation failed: {str(e)[:100]}. You can make changes and try again.")
                
                # Persist fail status to PostgreSQL if version is linked
                if state.version_id:
                    import asyncio
                    try:
                        asyncio.run(_fail_version_in_db(state.version_id, str(e)))
                    except Exception as db_err:
                        logger.error(f"Failed to trigger _fail_version_in_db async task: {db_err}")
                
                save_session(state)

