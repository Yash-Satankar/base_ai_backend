#!/usr/bin/env python3
"""
Dogfood driver: generate showcase schemas by running the ACTUAL product
end-to-end — conversation engine (multi-turn, with a clarifying round) →
async blueprint job → async schema job (generation + auto-iteration refinement
+ real-MySQL execution validation).

Nothing here is hand-written schema. This orchestrates the same calls the API
endpoints make.

Usage:
    python scripts/run_showcase.py [domain_slug ...]      # subset, or all if none

Env it sets for the run (all others come from .env):
    AI_PROVIDER=groq   SCHEMA_REFINE_ENABLED=True   MYSQL_EXEC_VALIDATION_ENABLED=True
    MYSQL_EXEC_VALIDATION_DSN=mysql://root:test@127.0.0.1:3306/
"""
from __future__ import annotations

import os
import sys
import json
import time
import asyncio
import shutil
from pathlib import Path

# ── run configuration — set BEFORE importing app ──────────────────
os.environ["SCHEMA_REFINE_ENABLED"] = "True"
os.environ["MYSQL_EXEC_VALIDATION_ENABLED"] = "True"
os.environ.setdefault("MYSQL_EXEC_VALIDATION_DSN", "mysql://root:test@127.0.0.1:3306/")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# Groq free tier caps at ~8000 tokens/minute — pace to it (wait out the TPM
# 429s) rather than skipping models.
os.environ.setdefault("GROQ_MAX_RATELIMIT_SLEEP", "75")

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPORT_PATH = HERE / "showcase_report.json"
FRONTEND_PUBLIC = BACKEND.parent / "base_ai_chat" / "public" / "showcase"

from app.core.config import settings                       # noqa: E402
from app.conversation import llm_client                    # noqa: E402
from app.services.conversation_service import (            # noqa: E402
    create_session, process_message, get_session,
    _blueprint_to_dict, _blueprint_to_requirement,
)
from app.conversation.blueprint_job import run_blueprint_job   # noqa: E402
from app.services.planner_service import generate_database_schema_for_job  # noqa: E402
from app.services.job_store import get_job_store           # noqa: E402
from app.engine.conversation_engine import ConversationStage  # noqa: E402


# ── the enterprise briefs ────────────────────────────────────────
BRIEFS: list[dict] = [
    {
        "slug": "financial-ledger",
        "domain": "Financial Ledger",
        "brief": (
            "We run a non-banking financial company (NBFC) in India with about 50 branches. "
            "I need the core double-entry accounting ledger. Requirements: a chart of accounts "
            "with account groups and nested sub-accounts; journal vouchers where each voucher "
            "has two or more debit/credit lines that must net to zero; sub-ledgers for loan "
            "accounts, fixed deposits and fixed assets that post into the general ledger; "
            "GST handling on fee and charge income (SGST, CGST, IGST with rates and amounts) "
            "and TDS deduction tracking on interest payouts; a denormalised running balance per "
            "GL account and per sub-ledger so statements don't have to sum from zero; accounting "
            "periods with a hard period-close that locks posted vouchers; bank accounts and a "
            "bank reconciliation workflow matching statement lines to voucher lines; a full "
            "immutable audit trail of who posted, approved and reversed each voucher, plus "
            "reversal vouchers that reference the original. Money is INR, two decimal places, "
            "never floats. Scale is millions of journal lines per year."
        ),
        "answers": (
            "Voucher types: receipt, payment, contra, journal, sales, purchase, credit note, "
            "debit note. Approval is two-step: maker posts as 'pending', checker approves to "
            "'posted'; only 'posted' vouchers hit the running balance. Period close is monthly "
            "and per-branch; a closed period rejects any new voucher with a date inside it. "
            "Sub-ledgers keep their own opening balance, movement lines and closing balance and "
            "reconcile to a single control account in the GL. Bank reconciliation needs an "
            "unmatched-items ageing view. Keep a life-cycle log for voucher status transitions "
            "(pending -> posted -> reversed) with who and when. We do not need multi-currency."
        ),
    },
    {
        "slug": "healthcare-ehr",
        "domain": "Healthcare (EHR)",
        "brief": (
            "Design the clinical database for a 300-bed multi-speciality hospital. It must cover: "
            "patient master with demographics, multiple identifiers (MRN, national ID, insurance "
            "member IDs), emergency contacts and next of kin; encounters spanning outpatient, "
            "inpatient admissions and emergency visits, each linked to an attending clinician, a "
            "department and (for inpatients) a bed; problem list and encounter diagnoses coded "
            "with ICD-10; procedures coded with CPT; medication orders and a separate medication "
            "administration record (who gave what dose when); lab orders with specimen tracking "
            "and structured results that carry reference ranges and abnormal flags; vital signs "
            "time series; documented allergies and intolerances with reaction and severity; care "
            "team membership per encounter; bed and ward management with admit/transfer/discharge "
            "movements; consent records per procedure; and a tamper-evident access audit log of "
            "every read and write against a patient chart for compliance. Timestamps must keep "
            "time of day. Realistic hospital scale."
        ),
        "answers": (
            "Patient identifiers are typed (MRN, Aadhaar, passport, insurer member id) and a "
            "patient can hold several of each over time, so model identifiers as their own table "
            "with valid-from/valid-to. Admit/transfer/discharge should be a movement table with "
            "one row per movement referencing from-bed and to-bed. Lab results: one order can "
            "have many result components, each with value, unit, reference low/high and a flag "
            "(normal/high/low/critical). Medication administration references the order and "
            "records scheduled vs actual time, dose given, route and the administering nurse. "
            "The access audit log is append-only and records user, patient, encounter, action "
            "(view/create/update), the table touched and a timestamp. Keep archive copies of "
            "patient and encounter records when they are amended."
        ),
    },
    {
        "slug": "saas-control-plane",
        "domain": "Multi-Tenant SaaS",
        "brief": (
            "I'm building the control plane for a B2B multi-tenant SaaS product and need the "
            "database. It must handle: organisations (tenants) and users, where a user can "
            "belong to more than one organisation with a distinct role in each; role-based "
            "access control with roles, granular permissions and role-permission bindings per "
            "organisation, including custom roles; subscription plans with feature entitlements "
            "and metered limits; an organisation's current subscription plus its history of plan "
            "changes; usage metering events aggregated into billing-period counters; invoices "
            "with line items, taxes and credits, and payment records against them; per-tenant and "
            "per-plan feature flags; API keys scoped to an organisation with last-used tracking "
            "and revocation; outbound webhook endpoints with delivery attempt logs; SSO / SAML "
            "connection config per organisation; and an audit log of security-relevant actions "
            "(member added, role changed, key issued, plan changed). Every tenant-owned table "
            "must carry the organisation id. Thousands of tenants."
        ),
        "answers": (
            "Membership is its own table (user_id, organisation_id, role_id, status, invited_by, "
            "joined_on) and is the thing RBAC hangs off. Permissions are strings like "
            "'billing.write' grouped by resource; a role binds a set of permissions within one "
            "organisation. Plan changes need effective-from/effective-to and the reason. Usage "
            "metering: raw events (organisation, metric, quantity, occurred_at) plus a rolled-up "
            "counter per organisation per metric per billing period. Invoices have status "
            "(draft/open/paid/void), currency, subtotal, tax total, total, and line items that "
            "reference either a plan or a usage metric. API keys store only a hash, a prefix for "
            "display, scopes, created_by, last_used_on and revoked_on. Webhook deliveries log "
            "endpoint, event type, response status, attempt number and next retry time."
        ),
    },
    {
        "slug": "logistics-freight",
        "domain": "Logistics",
        "brief": (
            "Design the operational database for a national parcel and freight carrier moving "
            "millions of shipments a month. Scope: customers and their contracted rate cards; "
            "shipment orders, each containing one or more parcels/consignments with weight, "
            "declared value and dimensions (length, width, height) and a computed volumetric "
            "weight; origin and destination addresses; a network of hubs and delivery branches; "
            "planned routes made of ordered legs between hubs, and the assignment of shipments to "
            "a route and a run; vehicles and drivers, and which driver/vehicle ran each leg; a "
            "high-volume tracking-event table recording every scan (picked up, arrived at hub, "
            "departed hub, out for delivery, delivered, delivery failed, returned) with location "
            "and timestamp; proof of delivery (signature, photo reference, recipient name, "
            "delivered_on); billing with invoices and line items derived from the rate card; "
            "exceptions and claims for lost or damaged parcels with a resolution workflow; and, "
            "for cross-border shipments, customs declarations and document references. Weights "
            "and money are decimals. Scan timestamps keep time of day."
        ),
        "answers": (
            "A shipment order has many consignments; each consignment has many tracking events; "
            "tracking events are append-only and are the highest-volume table, so index them on "
            "consignment and on scan time. Rate cards are versioned per customer with slabs "
            "(weight-from, weight-to, zone, price). A route is a template of ordered legs "
            "(sequence, from_hub, to_hub, expected_hours); a run is one dated execution of a "
            "route with an assigned vehicle and driver per leg. Delivery failure records a "
            "reason code and whether a re-attempt is scheduled. Claims have status "
            "(open/investigating/approved/rejected/paid), a claimed amount and an approved "
            "amount. Customs declaration holds HS codes per line, declared value, duties and "
            "the linked document references. Keep a status life-cycle log on shipment orders and "
            "on claims."
        ),
    },
]

PROCEED = "Generate Blueprint"


def _job_result(store, job_id):
    job = store.get(job_id)
    if not job:
        raise RuntimeError(f"job {job_id} vanished")
    if job["status"] == "failed":
        raise RuntimeError(f"job failed: {job.get('error')}")
    return job


async def run_one(b: dict) -> dict:
    slug, domain = b["slug"], b["domain"]
    print(f"\n{'='*70}\n {domain}  ({slug})\n{'='*70}")
    t0 = time.time()
    store = get_job_store()

    # 1) real multi-turn conversation (guest session, Redis-backed)
    state = await create_session(db=None, user=None, project_id=None)
    sid = state.session_id
    print(f"  session_id = {sid}")

    r = await process_message(sid, b["brief"], db=None, user=None)
    print(f"  turn 1 -> stage={r.get('stage')}  (clarifying questions returned)")

    r = await process_message(sid, b["answers"], db=None, user=None)
    print(f"  turn 2 -> stage={r.get('stage')}")

    if r.get("mode") != "blueprint" and r.get("stage") not in ("compiling",):
        r = await process_message(sid, PROCEED, db=None, user=None)
        print(f"  turn 3 -> stage={r.get('stage')}  mode={r.get('mode')}")

    # 2) blueprint compile job (what the frontend's poll would drive)
    requirement = r.get("requirement") or get_session(sid).requirement_summary
    bp_job = store.create(requirement, None)
    print("  running blueprint job (L1-L8)…")
    run_blueprint_job(bp_job, sid, requirement)
    _job_result(store, bp_job)
    state = get_session(sid)
    if not state.blueprint:
        raise RuntimeError("blueprint job did not attach a blueprint to the session")
    bp_tables = sum(len(m.get("tables", [])) for m in _blueprint_to_dict(state.blueprint)["modules"])
    print(f"  blueprint ready: {len(state.blueprint.modules)} modules, ~{bp_tables} tables planned")

    # 3) confirm -> schema generation job (generation + refine + exec-validate)
    r = await process_message(sid, "YES", db=None, user=None)
    state = get_session(sid)
    blueprint_dict = r.get("blueprint") or _blueprint_to_dict(state.blueprint)
    gen_requirement = r.get("requirement") or _blueprint_to_requirement(state.blueprint)

    sch_job = store.create(gen_requirement, blueprint_dict)
    print("  running schema generation job (generate -> refine -> mysql-validate)…")
    generate_database_schema_for_job(
        job_id=sch_job,
        requirement=gen_requirement,
        blueprint=blueprint_dict,
        session_id=sid,
        mode="schema",
    )
    job = _job_result(store, sch_job)
    result = job["result"]
    # generate_database_schema_for_job writes sql_file_path/pdf_file_path onto
    # the PERSISTED session as its last step — the `state` fetched at line 229
    # (before the job ran) is now stale and will never see them. Re-fetch.
    state = get_session(sid)

    v = result["validation"]
    execu = v.get("execution") or {}
    refine = v.get("refinement") or {}
    meta = result["metadata"]
    gen_sum = result["generation_summary"]

    # combined cost: refinement cost + the job's own token-usage cost (task's definition)
    tu = meta.get("token_usage", {})
    _rate_model = (settings.TOGETHER_MODEL if settings.AI_PROVIDER == "together"
                   else meta.get("ai_model") or settings.GROQ_MODEL)
    rin, rout = llm_client._MODEL_RATES.get(_rate_model, llm_client._MODEL_RATES["_default"])
    job_token_cost = round(
        tu.get("input_tokens", 0) / 1_000_000 * rin + tu.get("output_tokens", 0) / 1_000_000 * rout, 6
    )
    combined_cost = round(job_token_cost + float(refine.get("total_cost_usd", 0.0)), 6)

    # copy the real generated files into the frontend
    FRONTEND_PUBLIC.mkdir(parents=True, exist_ok=True)
    sql_src = getattr(state, "sql_file_path", None)
    pdf_src = getattr(state, "pdf_file_path", None)
    sql_out = pdf_out = None
    if sql_src and Path(sql_src).exists():
        sql_out = f"{slug}.sql"
        shutil.copyfile(sql_src, FRONTEND_PUBLIC / sql_out)
    if pdf_src and Path(pdf_src).exists():
        pdf_out = f"{slug}.pdf"
        shutil.copyfile(pdf_src, FRONTEND_PUBLIC / pdf_out)

    score = v["score"]
    grade = v["grade"]
    blocking = v["critical_issues"] + v["high_issues"]
    tables_planned = gen_sum.get("tables_planned") or 0
    table_count = len(v["tables_found"])
    # completeness measured on the FINAL (post-refinement) schema — not the
    # generation-time ratio, which can be higher if the refiner later shrank it.
    completeness_ratio = round(table_count / tables_planned, 4) if tables_planned else None
    gate_ratio = gen_sum.get("completeness_ratio")  # what the job's gate saw
    adv = execu.get("advisory_issue_count")
    adv_threshold = settings.SCHEMA_REFINE_ADVISORY_THRESHOLD
    min_ratio = settings.SCHEMA_COMPLETENESS_MIN_RATIO

    # A schema is shippable ONLY if: it converged on its own terms, MySQL accepts
    # it with zero enterprise errors, advisories are strictly BELOW the threshold
    # (not "at" it), and completeness is at/above the min ratio.
    reasons = []
    if not refine.get("converged"):
        reasons.append("did not converge")
    if refine.get("degraded"):
        reasons.append("ran degraded")
    if not execu.get("success"):
        reasons.append("MySQL execution not clean")
    if (execu.get("error_issue_count") or 0) > 0:
        reasons.append(f"{execu.get('error_issue_count')} enterprise error(s)")
    if adv is not None and adv >= adv_threshold:
        reasons.append(f"advisories {adv} >= threshold {adv_threshold} (at/above)")
    if completeness_ratio is not None and completeness_ratio < min_ratio:
        reasons.append(f"completeness {completeness_ratio:.0%} < {min_ratio:.0%}")
    if gen_sum.get("is_complete") is False:
        # A high table-count ratio can mask a permanently-failed module if
        # other modules over-delivered — is_complete also requires zero
        # failed_modules, which the ratio alone doesn't catch.
        reasons.append("generation job reported incomplete (a module failed)")
    if blocking > 0:
        reasons.append(f"{blocking} structural blocking issue(s)")
    if score < 90:
        reasons.append(f"structural score {score} < 90")
    shippable = not reasons

    rec = {
        "slug": slug,
        "domain": domain,
        "session_id": sid,
        "shippable": shippable,
        "not_shippable_reasons": reasons,
        "table_count": table_count,
        "tables_planned": tables_planned,
        "completeness_ratio": completeness_ratio,
        "gate_completeness_ratio": gate_ratio,
        "completeness_min_ratio": min_ratio,
        "is_complete": gen_sum.get("is_complete"),
        "iterations_used": refine.get("iterations_used"),
        "converged": refine.get("converged"),
        "degraded": refine.get("degraded"),
        "structural_score": score,
        "structural_grade": grade,
        "structural_blocking_issues": blocking,
        "structural_breakdown": v.get("scores_breakdown"),
        "mysql_execution": {
            "success": execu.get("success"),
            "skipped": execu.get("skipped"),
            "ddl_error_count": len(execu.get("ddl_errors", []) or []),
            "enterprise_error_count": execu.get("error_issue_count"),
            "advisory_count": adv,
            "advisory_threshold": adv_threshold,
            "enterprise_score": execu.get("enterprise_score"),
            "summary": execu.get("summary"),
        },
        "refinement_remaining_issues": refine.get("remaining_issues", []),
        "combined_cost_usd": combined_cost,
        "_cost_parts": {"job_token_cost": job_token_cost,
                        "refinement_cost": float(refine.get("total_cost_usd", 0.0))},
        "elapsed_seconds": round(time.time() - t0, 1),
        "files": {"sql": sql_out, "pdf": pdf_out},
        "enterprise_findings": [
            {"severity": i["severity"], "category": i["category"], "message": i["message"][:200]}
            for i in (execu.get("issues") or [])
        ],
    }

    cr = f"{completeness_ratio:.0%}" if completeness_ratio is not None else "?"
    print(f"  DONE  tables={table_count}/{tables_planned} ({cr})  score={score}/{grade}  "
          f"blocking={blocking}  adv={adv}/{adv_threshold}  iters={rec['iterations_used']}  "
          f"converged={rec['converged']}  degraded={rec['degraded']}  "
          f"mysql=({rec['mysql_execution']['summary']})  cost=${combined_cost:.4f}  "
          f"{rec['elapsed_seconds']}s  ->  {'SHIPPABLE' if shippable else 'REJECT: ' + '; '.join(reasons)}")
    return rec


async def main():
    _model = settings.TOGETHER_MODEL if settings.AI_PROVIDER == "together" else settings.GROQ_MODEL
    print("── run configuration ──")
    print(f"  AI_PROVIDER                   = {settings.AI_PROVIDER}")
    print(f"  MODEL (primary)              = {_model}")
    print(f"  SCHEMA_REFINE_ENABLED        = {settings.SCHEMA_REFINE_ENABLED}")
    print(f"  SCHEMA_REFINE_MAX_ITERATIONS = {settings.SCHEMA_REFINE_MAX_ITERATIONS}")
    print(f"  SCHEMA_REFINE_ADVISORY_THRESHOLD = {settings.SCHEMA_REFINE_ADVISORY_THRESHOLD}")
    print(f"  MYSQL_EXEC_VALIDATION_ENABLED = {settings.MYSQL_EXEC_VALIDATION_ENABLED}")
    print(f"  MYSQL_EXEC_VALIDATION_DSN     = {settings.MYSQL_EXEC_VALIDATION_DSN}")
    assert settings.AI_PROVIDER in ("groq", "together"), f"unexpected provider {settings.AI_PROVIDER!r}"
    assert settings.SCHEMA_REFINE_ENABLED is True
    assert settings.MYSQL_EXEC_VALIDATION_ENABLED is True

    wanted = set(sys.argv[1:])
    briefs = [b for b in BRIEFS if not wanted or b["slug"] in wanted]

    report = []
    if REPORT_PATH.exists():
        report = json.loads(REPORT_PATH.read_text())
        report = [r for r in report if r["slug"] not in {b["slug"] for b in briefs}]

    for b in briefs:
        try:
            rec = await run_one(b)
        except Exception as e:
            import traceback
            traceback.print_exc()
            rec = {"slug": b["slug"], "domain": b["domain"], "error": str(e)}
        report.append(rec)
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        print(f"  (report written: {REPORT_PATH})")

    print("\n\n════════ SUMMARY ════════")
    for r in report:
        if "error" in r:
            print(f"  {r['domain']:22} ERROR: {r['error']}")
            continue
        print(f"  {r['domain']:22} tables={r['table_count']:<3} "
              f"score={r['structural_score']}/{r['structural_grade']} "
              f"blocking={r['structural_blocking_issues']} "
              f"iters={r['iterations_used']} converged={r['converged']} "
              f"degraded={r['degraded']} "
              f"mysql_err={r['mysql_execution']['ddl_error_count']}/"
              f"ent_err={r['mysql_execution']['enterprise_error_count']}/"
              f"adv={r['mysql_execution']['advisory_count']} "
              f"${r['combined_cost_usd']:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
