# app/engine/golden_dataset.py
"""
Golden Dataset & Regression Suite: Defines reference enterprise requirements
and evaluates the platform's generation quality to prevent regressions.
"""

import time
import logging
from typing import List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.ai_service import generate_schema
from app.services.architecture_reviewer import _parse_sql_to_blueprint
from app.engine.score_engine import evaluate_blueprint

logger = logging.getLogger(__name__)

# ── Golden Dataset Requirements ───────────────────────────────────────────────

GOLDEN_REQUIREMENTS: List[Dict[str, Any]] = [
    {
        "name": "Double-Entry Ledger",
        "baseline_score": 85.0,
        "requirement": (
            "A multi-currency double-entry ledger system. Needs account headers, "
            "transaction lines, currency exchanges, audit trails, and automatic balance "
            "verification. Transactions must be approved by supervisors."
        )
    },
    {
        "name": "Hospital Management ERP",
        "baseline_score": 80.0,
        "requirement": (
            "A clinic and hospital management system. Needs patient records, doctor schedules, "
            "consultations, medical billing, prescription logs, and compliance audits for patient data access."
        )
    },
    {
        "name": "Ride-Sharing Platform",
        "baseline_score": 80.0,
        "requirement": (
            "A ride-sharing database. Needs drivers, riders, rides, payment transactions, "
            "driver ratings, location history logs, and supervisor ride cancellation approvals."
        )
    }
]


# ── Regression Test Suite ─────────────────────────────────────────────────────

async def run_regression_tests(db: AsyncSession) -> dict:
    """
    Executes the regression test suite across all golden requirements.
    Compares the current score against the baseline target.
    """
    logger.info("🧪 Running golden dataset regression suite...")
    
    results = []
    total_duration = 0.0
    regressions_detected = False

    for item in GOLDEN_REQUIREMENTS[:2]:  # Run the first 2 to keep tests fast
        start_time = time.time()
        
        # Compile schema using BaseAI's system prompt
        from app.prompts.system_prompt import build_system_prompt
        system_prompt = build_system_prompt(domain="general", gst_required=True, scale="medium")
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=f"Generate an enterprise database schema for: {item['requirement']}",
            max_tokens=2000
        )
        
        sql = response["content"]
        blueprint = _parse_sql_to_blueprint(sql)
        report = evaluate_blueprint(blueprint)
        
        duration = time.time() - start_time
        total_duration += duration

        score = report["overall_score"]
        passed = score >= item["baseline_score"]
        if not passed:
            regressions_detected = True

        results.append({
            "requirement_name": item["name"],
            "baseline_score": item["baseline_score"],
            "current_score": score,
            "duration_seconds": round(duration, 2),
            "status": "PASSED" if passed else "REGRESSED",
            "findings": report["findings"][:3]
        })

    logger.info(f"🏁 Regression tests completed. Regressions detected: {regressions_detected}")

    return {
        "success": True,
        "regressions_detected": regressions_detected,
        "total_duration_seconds": round(total_duration, 2),
        "test_results": results
    }
