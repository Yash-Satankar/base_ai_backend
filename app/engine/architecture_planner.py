# app/engine/architecture_planner.py

import json
import re
import logging
from app.services.ai_service import generate_schema

logger = logging.getLogger(__name__)

# Minimum table count for a blueprint to be considered valid.
# Anything below this triggers a retry or a domain-aware fallback.
MIN_TABLES_THRESHOLD = 20


def generate_deep_blueprint(
    requirement: str,
    domain: str,
    gst_required: bool,
    scale: str,
) -> dict:
    """
    Generate a project-specific blueprint using AI.
    The schema structure is determined by the project's actual needs.
    Retries once with a stronger prompt if the first attempt yields too few tables.
    Falls back to a domain-aware hardcoded blueprint if both AI attempts fail.
    """
    # Truncate very long requirements to avoid token overflow in the blueprint call.
    # The planner will receive the full blueprint + requirement anyway.
    req_for_blueprint = requirement[:3000] if len(requirement) > 3000 else requirement

    # ── Attempt 1: full requirement in user_prompt ───────────────
    result = _try_generate(req_for_blueprint, domain, gst_required, scale, attempt=1)
    total_tables = sum(len(m.get("tables", [])) for m in result.get("modules", []))

    if total_tables >= MIN_TABLES_THRESHOLD:
        logger.info(f"✅ Blueprint attempt 1 OK: {len(result['modules'])} modules, {total_tables} tables")
        return result

    logger.warning(
        f"⚠️ Blueprint attempt 1 returned only {total_tables} tables "
        f"(need ≥{MIN_TABLES_THRESHOLD}). Retrying with stronger prompt..."
    )

    # ── Attempt 2: stripped-down requirement + explicit module list ──
    result2 = _try_generate(req_for_blueprint, domain, gst_required, scale, attempt=2)
    total2 = sum(len(m.get("tables", [])) for m in result2.get("modules", []))

    if total2 >= MIN_TABLES_THRESHOLD:
        logger.info(f"✅ Blueprint attempt 2 OK: {len(result2['modules'])} modules, {total2} tables")
        return result2

    # If attempt 2 returned MORE tables than attempt 1, prefer it even if
    # it's still under the threshold (edge-case: both attempts partially worked).
    best = result2 if total2 > total_tables else result

    # ── Fallback: domain-aware hardcoded blueprint ───────────────
    logger.error(
        f"❌ Both blueprint attempts returned too few tables "
        f"(attempt1={total_tables}, attempt2={total2}). Using domain fallback."
    )
    fallback = _domain_fallback(requirement, domain, gst_required, scale)
    fallback_tables = sum(len(m.get("tables", [])) for m in fallback.get("modules", []))
    logger.info(f"📦 Domain fallback: {len(fallback['modules'])} modules, {fallback_tables} tables")
    return fallback


# ── Internal helpers ─────────────────────────────────────────────

def _try_generate(requirement: str, domain: str, gst_required: bool, scale: str, attempt: int) -> dict:
    """Single AI call for blueprint generation using the Principal Architect's mental model."""

    # Domain-specific module lists (attempt 1: from requirement; attempt 2: explicit fallback list)
    DOMAIN_MODULE_HINTS = {
        "aviation": (
            "Infrastructure, User & Identity, Airline & Fleet, Flight Operations, "
            "Passenger Services, Baggage Handling, Cargo Operations, Gate & Resource Management, "
            "Aircraft Maintenance, Staff & Workforce, Ground Operations, Terminal & Asset Management, "
            "Security & Immigration, Finance & Revenue, Notifications & Alerts, Audit & Compliance"
        ),
        "airport": (
            "Infrastructure, User & Identity, Airline & Fleet, Flight Operations, "
            "Passenger Services, Baggage Handling, Cargo Operations, Gate & Resource Management, "
            "Aircraft Maintenance, Staff & Workforce, Ground Operations, Terminal & Asset Management, "
            "Security & Immigration, Finance & Revenue, Notifications & Alerts, Audit & Compliance"
        ),
        "e_commerce": (
            "Infrastructure, User & Identity, Vendor & KYC, Product Catalog, Inventory & Warehouse, "
            "Customer Management, Order Management, Logistics & Delivery, Finance & Settlements, "
            "Marketing & Promotions, Customer Support, Notification & Communication, "
            "Configuration Management, Audit & Compliance, Analytics & Reporting"
        ),
        "healthcare": (
            "Infrastructure, User & Identity, Patient Management, Doctor & Staff, Appointments & Scheduling, "
            "Clinical Records, Ward & Bed Management, Lab & Diagnostics, Pharmacy & Inventory, "
            "Billing & Finance, Insurance & Claims, Emergency & Casualty, "
            "Notification & Communication, Audit & Compliance"
        ),
        "logistics": (
            "Infrastructure, User & Identity, Fleet & Vehicle Management, Driver Management, "
            "Customer & Contracts, Shipment & Cargo, Route & Trip Management, Warehouse Management, "
            "Tracking & Events, Finance & Billing, Vendor & Partner, "
            "Notification & Communication, Audit & Compliance"
        ),
        "hr": (
            "Infrastructure, User & Identity, Employee Master, Department & Designation, "
            "Attendance & Time Tracking, Leave Management, Payroll & Salary, Recruitment, "
            "Appraisal & Performance, Training & Development, Benefits & Compensation, "
            "Notification & Communication, Audit & Compliance"
        ),
        "financial": (
            "Infrastructure, User & Identity, Account Management, Loan & Credit, "
            "Transaction & Ledger, EMI & Repayment, Settlement & Reconciliation, "
            "Compliance & KYC, Notification & Alerts, Audit & Compliance"
        ),
        "multi_tenant_saas": (
            "Infrastructure, Tenant Management, User & Identity, Subscription & Billing, "
            "Feature & Configuration, Core Business Module, Analytics & Reporting, "
            "Notification & Communication, Support & Helpdesk, Audit & Compliance"
        ),
        "security_agency": (
            "Infrastructure, User & Identity, Guard & Employee, Campus & Client Management, "
            "Duty Roster & Scheduling, Attendance & Patrol, Salary & Payroll, "
            "SOS & Incident Response, Equipment & Uniform, Contract Management, "
            "Finance & Billing, Notification & Communication, Audit & Compliance"
        ),
        "real_estate": (
            "Infrastructure, User & Identity, Broker & Agent, Property Listings, "
            "Client & Requirements, Property Visits, Deals & Transactions, "
            "Finance & Commission, Document Management, Notification & Communication, Audit & Compliance"
        ),
    }

    # Find best matching module hint
    domain_lower = domain.lower()
    module_hint_modules = DOMAIN_MODULE_HINTS.get(domain_lower, "")
    if not module_hint_modules:
        for k, v in DOMAIN_MODULE_HINTS.items():
            if k in domain_lower or domain_lower in k:
                module_hint_modules = v
                break
    if not module_hint_modules:
        module_hint_modules = (
            "Infrastructure, User & Identity, Core Entity A, Core Entity B, "
            "Transactions & Events, Finance & Payments, Notifications & Communication, Audit & Compliance"
        )

    if attempt == 1:
        module_guidance = f"""Generate modules covering ALL aspects of this {domain.replace('_', ' ')} system.
For this domain, the expected modules are: {module_hint_modules}
Adapt 100% based on the actual requirement — add or remove modules as needed."""
    else:
        module_guidance = f"""FALLBACK ATTEMPT: You MUST generate at least 12 modules.
Use EXACTLY these module names (adapt table names to the requirement):
{chr(10).join(f'{i+1}. {m.strip()}' for i, m in enumerate(module_hint_modules.split(','))[:14])}
Each module MUST have at least 6 tables."""

    system_prompt = f"""You are a Principal Database Architect designing a production {domain.replace('_', ' ')} system.
You have designed 32 production databases. You NEVER generate generic schemas.
Your task: produce a JSON blueprint — the canonical architecture before any SQL is written.

THE ARCHITECT'S 10 QUESTIONS (answer for each entity):
1. What is the MASTER RECORD?          → _header_all (MINIMUM 15 columns when generated)
2. What EVENTS does it generate?        → _transaction_all (append-only)
3. What SETTINGS does it have?          → _configuration_all
4. What CHANGES STATE over time?        → _life_cycle_all (state machine audit trail)
5. What must be PRESERVED HISTORICALLY? → _archive_all (identical mirror + archived_on)
6. What CHILD RECORDS does it own?      → _details_all or _item_all
7. What is COUNTED in real-time?        → aggregate counter columns on _header_all row
8. What is CALENDAR-BASED?              → numbered day_1..day_31 column table
9. What has MULTIPLE APPROVAL stages?   → inline approval_1..N columns on transaction row
10. What FAILS and needs tracking?      → _failed_all or _rejected_all tables

BLUEPRINT REQUIREMENTS:
- Enterprise systems: MINIMUM 14 modules, MINIMUM 7 tables per module (≥100 tables total)
- Medium systems: MINIMUM 10 modules, MINIMUM 6 tables per module (≥60 tables total)
- Simple systems: MINIMUM 6 modules, MINIMUM 5 tables per module (≥30 tables total)
- This is a {scale} scale system: {'Enterprise requirements apply (≥14 modules)' if scale == 'large' else 'Standard requirements apply'}

TABLE NAMING TAXONOMY (mandatory):
_header_all        = master entity record
_transaction_all   = event/activity log (append-only)
_archive_all       = historical mirror of _header_all
_life_cycle_all    = state transition audit trail
_configuration_all = per-entity or per-context settings
_details_all       = child line-item records
_calendar_all      = monthly numbered-day-column tables
_failed_all        = rejected/failed records

PROPRIETARY RULES (apply all that are relevant):
- unique_id_header_all: ALWAYS in Infrastructure module (centralised business ID registry)
- factory_reset_all: global feature flags singleton — always in Infrastructure
- Three-layer preservation: _header_all + _archive_all + _life_cycle_all for complex entities
- Aggregate counters ON header rows (total_orders_count, total_flights_count, etc.)
- month_year VARCHAR(7) on EVERY _transaction_all table
- Approval chains: inline approval_1_by..approval_N_by on transaction rows
- DECIMAL(10,2) for ALL money, closing_balance on ledger transactions
- Every table: ENGINE=InnoDB, DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
- created_on / modified_on / *_on columns are DATETIME, never DATE
- NO duplicate table names across modules

{module_guidance}

DOMAIN: {domain.replace('_', ' ').title()}
GST REQUIRED: {gst_required}
SCALE: {scale}

Return ONLY valid JSON — no markdown, no explanation.

JSON structure:
{{
  "project_name": "Specific Descriptive Project Name",
  "description": "one precise sentence describing this system",
  "domain": "{domain}",
  "gst_required": {str(gst_required).lower()},
  "scale": "{scale}",
  "modules": [
    {{
      "name": "Module Name",
      "description": "precise description of what this module manages",
      "tables": [
        {{"name": "entity_header_all", "purpose": "specific purpose — not generic"}},
        {{"name": "entity_archive_all", "purpose": "historical mirror of entity_header_all"}},
        {{"name": "entity_life_cycle_all", "purpose": "state machine audit trail"}},
        {{"name": "entity_transaction_all", "purpose": "specific event/activity log purpose"}},
        {{"name": "entity_configuration_all", "purpose": "per-entity configuration"}}
      ]
    }}
  ]
}}"""

    user_prompt = f"""Design the COMPLETE database architecture for this specific system.
Do NOT use generic table names. Every table name must reflect its exact business purpose.

REQUIREMENT (read every line — this is a complex enterprise system):
{requirement[:3000]}

ARCHITECT'S ANALYSIS REQUIRED:
1. List all core ENTITIES in this system (each needs a _header_all table)
2. For each entity, identify which of the 10 architecture questions apply
3. Generate tables based on those answers — NOT from a generic template
4. For enterprise/large systems: minimum 14 modules, 7+ tables each (100+ tables total)
5. For every entity that changes state: add _life_cycle_all
6. For every entity that needs audit history: add _archive_all
7. For every entity that generates events: add _transaction_all
8. Include calendar tables (day_1..day_31 pattern) for scheduling-heavy modules
9. Include _failed_all or _rejected_all for approval-workflow entities
10. Include resource booking tables with temporal conflict prevention

Generate the most comprehensive, production-grade blueprint possible for this domain."""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        content = response["content"].strip()
        content = _strip_markdown(content)
        data = json.loads(content)
        return data

    except json.JSONDecodeError as e:
        logger.error(f"Blueprint attempt {attempt} — JSON parse failed: {e}")
        return {}
    except Exception as e:
        logger.error(f"Blueprint attempt {attempt} — AI call failed: {e}")
        return {}


def _strip_markdown(content: str) -> str:
    """Remove ```json ... ``` fences from AI output."""
    content = content.strip()
    # Handle ```json...``` or ```...```
    if "```" in content:
        parts = content.split("```")
        # Take the middle part (the actual JSON)
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                return part
        # Fallback: take second segment
        if len(parts) >= 2:
            candidate = parts[1].strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            return candidate
    return content


# ── Domain-aware fallback blueprints ────────────────────────────

def _domain_fallback(requirement: str, domain: str, gst_required: bool, scale: str) -> dict:
    """
    Select the best hardcoded fallback blueprint based on detected domain.
    All fallbacks have ≥80 tables spread across ≥12 modules.
    """
    desc = requirement[:120] + ("..." if len(requirement) > 120 else "")

    domain_lower = domain.lower()

    if any(k in domain_lower for k in ["aviation", "airport", "airline", "flight"]):
        return _aviation_fallback(desc, gst_required, scale)
    elif "e_commerce" in domain_lower or "ecommerce" in domain_lower or "commerce" in domain_lower:
        return _ecommerce_fallback(desc, gst_required, scale)
    elif "health" in domain_lower or "medical" in domain_lower or "hospital" in domain_lower:
        return _healthcare_fallback(desc, gst_required, scale)
    elif "logistic" in domain_lower or "delivery" in domain_lower or "transport" in domain_lower:
        return _logistics_fallback(desc, gst_required, scale)
    elif "hr" in domain_lower or "payroll" in domain_lower or "employee" in domain_lower:
        return _hr_fallback(desc, gst_required, scale)
    elif "finance" in domain_lower or "banking" in domain_lower or "fintech" in domain_lower:
        return _finance_fallback(desc, gst_required, scale)
    elif "saas" in domain_lower or "multi_tenant" in domain_lower:
        return _saas_fallback(desc, gst_required, scale)
    else:
        return _generic_enterprise_fallback(desc, domain, gst_required, scale)


def _aviation_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    """Airport Operations & Aviation Management Platform — 16 modules, 120+ tables."""
    return {
        "project_name": "Airport Operations & Aviation Management Platform",
        "description": desc,
        "domain": "aviation",
        "gst_required": gst_required,
        "scale": scale,
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System registry, feature flags, configuration, and DB self-monitoring",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised business ID registry for all airport entities"},
                    {"name": "factory_reset_all", "purpose": "Global platform feature flags and kill-switches singleton"},
                    {"name": "table_inside_table_all", "purpose": "DB table capacity monitoring and row-limit alerting"},
                    {"name": "app_version_configuration_all", "purpose": "App version control and force-update rules per client type"},
                    {"name": "platform_configuration_all", "purpose": "Platform-wide runtime configuration and global settings"},
                    {"name": "airport_configuration_all", "purpose": "Airport-specific configuration: ICAO code, terminals count, runways, timezone"},
                ]
            },
            {
                "name": "User & Identity Management",
                "description": "All platform users, roles, permissions, sessions, and OTP",
                "tables": [
                    {"name": "user_header_all", "purpose": "All platform users — airport staff, airline operators, vendors, security, customs"},
                    {"name": "user_archive_all", "purpose": "Historical mirror of user_header_all"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit trail (active, suspended, terminated, reinstated)"},
                    {"name": "role_header_all", "purpose": "User roles — airport admin, airline operator, security officer, customs, maintenance"},
                    {"name": "permission_header_all", "purpose": "Granular feature and module level permissions per role"},
                    {"name": "user_role_transaction_all", "purpose": "Role assignment and revocation history with approver"},
                    {"name": "otp_transaction_all", "purpose": "OTP generation and verification log with expiry and rate limiting"},
                    {"name": "session_header_all", "purpose": "Active user sessions, device tokens, and biometric auth records"},
                ]
            },
            {
                "name": "Airline & Fleet Management",
                "description": "Airlines, aircraft, routes, leases, and code-share agreements",
                "tables": [
                    {"name": "airline_header_all", "purpose": "Airline master — IATA/ICAO code, name, type (domestic/international), contact"},
                    {"name": "airline_archive_all", "purpose": "Historical mirror of airline_header_all"},
                    {"name": "airline_life_cycle_all", "purpose": "Airline status audit trail (active, suspended, expelled)"},
                    {"name": "aircraft_header_all", "purpose": "Aircraft master — tail number, type, capacity, maintenance due dates, flight hours"},
                    {"name": "aircraft_archive_all", "purpose": "Historical mirror of aircraft_header_all"},
                    {"name": "aircraft_life_cycle_all", "purpose": "Aircraft status audit trail (active, grounded, maintenance, decommissioned)"},
                    {"name": "aircraft_configuration_all", "purpose": "Aircraft seat configuration per class (first/business/economy) with counts"},
                    {"name": "route_header_all", "purpose": "Airline route master — origin airport, destination airport, distance, type"},
                    {"name": "codeshare_header_all", "purpose": "Code-share flight agreements between airlines — marketed vs operating airline"},
                    {"name": "gate_lease_header_all", "purpose": "Airline gate leasing agreements — gate assignment, terminal, lease period, fees"},
                    {"name": "ground_handling_agreement_header_all", "purpose": "Airline-to-agency ground handling agreements with scope and pricing"},
                ]
            },
            {
                "name": "Flight Operations",
                "description": "Flight scheduling, status, delays, cancellations, diversions, and turnaround",
                "tables": [
                    {"name": "flight_header_all", "purpose": "Flight master — flight number, airline, aircraft, origin, destination, scheduled times"},
                    {"name": "flight_archive_all", "purpose": "Historical mirror of flight_header_all"},
                    {"name": "flight_life_cycle_all", "purpose": "Flight status audit trail (scheduled→gate_open→boarding→closed→taxiing→departed→arrived)"},
                    {"name": "flight_schedule_header_all", "purpose": "Recurring flight schedule master — frequency, days of week, seasonal validity"},
                    {"name": "flight_delay_transaction_all", "purpose": "Delay log — delay_code, delay_minutes, reason, responsible_party, notified_passengers"},
                    {"name": "flight_cancellation_header_all", "purpose": "Cancellation records — reason, authority, refund_triggered, rebooking_triggered"},
                    {"name": "flight_diversion_header_all", "purpose": "Diversion events — diverted_to_airport, reason, landed_time, passenger_handling_plan"},
                    {"name": "turnaround_header_all", "purpose": "Aircraft turnaround plan — cleaning, fuelling, catering, boarding timeline"},
                    {"name": "turnaround_transaction_all", "purpose": "Turnaround activity log — task, start_time, end_time, crew_member, status"},
                    {"name": "crew_assignment_header_all", "purpose": "Crew assignment to flights — pilot, co-pilot, cabin crew, role, duty status"},
                    {"name": "crew_assignment_life_cycle_all", "purpose": "Crew assignment changes audit trail"},
                ]
            },
            {
                "name": "Passenger Services",
                "description": "Passenger profiles, bookings, check-in, boarding, and special assistance",
                "tables": [
                    {"name": "passenger_header_all", "purpose": "Passenger master — name, passport, nationality, frequent_flyer, special_category"},
                    {"name": "passenger_archive_all", "purpose": "Historical mirror of passenger_header_all"},
                    {"name": "passenger_life_cycle_all", "purpose": "Passenger status audit trail per journey"},
                    {"name": "booking_header_all", "purpose": "Booking master — PNR, flight, passenger, class, fare, booking_channel"},
                    {"name": "booking_archive_all", "purpose": "Historical mirror of booking_header_all"},
                    {"name": "booking_life_cycle_all", "purpose": "Booking state audit trail (confirmed→checked_in→boarded→completed/cancelled/no_show)"},
                    {"name": "seat_assignment_header_all", "purpose": "Seat allocation per passenger per flight with upgrade history"},
                    {"name": "checkin_transaction_all", "purpose": "Check-in events — channel (web/kiosk/counter), agent, time, seat confirmed, bag count"},
                    {"name": "boarding_transaction_all", "purpose": "Boarding scan events — boarding_time, gate, method (biometric/barcode/manual)"},
                    {"name": "passenger_assistance_header_all", "purpose": "Special assistance requests — wheelchair, medical, unaccompanied_minor, VIP escort"},
                    {"name": "lounge_access_transaction_all", "purpose": "Lounge entry/exit log — passenger, lounge, entry_time, exit_time, access_type"},
                    {"name": "passenger_disruption_header_all", "purpose": "Disruption handling — missed connection, rebooking, compensation, hotel voucher"},
                ]
            },
            {
                "name": "Baggage Handling",
                "description": "Baggage from check-in to delivery including transfers, screening, and claims",
                "tables": [
                    {"name": "baggage_header_all", "purpose": "Baggage master — barcode, passenger, flight, weight, dimensions, type, status"},
                    {"name": "baggage_archive_all", "purpose": "Historical mirror of baggage_header_all"},
                    {"name": "baggage_life_cycle_all", "purpose": "Baggage status audit trail (checked_in→screened→loaded→in_transit→delivered)"},
                    {"name": "baggage_movement_transaction_all", "purpose": "Every baggage scan event — location, belt_id, handler, timestamp, action"},
                    {"name": "baggage_transfer_header_all", "purpose": "Connecting flight baggage transfer records — from_flight, to_flight, transfer_time"},
                    {"name": "lost_baggage_header_all", "purpose": "Lost baggage reports — PIR number, last_known_location, search_status"},
                    {"name": "lost_baggage_life_cycle_all", "purpose": "Lost baggage investigation status audit trail"},
                    {"name": "damaged_baggage_header_all", "purpose": "Damage reports — damage_type, assessed_value, liability_party, claim_status"},
                    {"name": "baggage_claim_header_all", "purpose": "Claim submissions — claim_type, claim_amount, compensation_offered, resolution"},
                    {"name": "baggage_belt_assignment_transaction_all", "purpose": "Belt assignment per flight for arrivals carousel allocation"},
                ]
            },
            {
                "name": "Cargo Operations",
                "description": "Cargo bookings, warehousing, customs, dangerous goods, and international logistics",
                "tables": [
                    {"name": "cargo_shipper_header_all", "purpose": "Cargo shipper/consignee master — company, IATA cargo agent code, certifications"},
                    {"name": "cargo_shipment_header_all", "purpose": "Shipment master — AWB number, type (standard/hazardous/perishable/live_animals), weight, value"},
                    {"name": "cargo_shipment_archive_all", "purpose": "Historical mirror of cargo_shipment_header_all"},
                    {"name": "cargo_shipment_life_cycle_all", "purpose": "Shipment status audit trail (received→screened→customs→warehouse→loaded→dispatched)"},
                    {"name": "cargo_movement_transaction_all", "purpose": "Every warehouse transfer and scan — location, handler, timestamp, action_type"},
                    {"name": "cargo_warehouse_header_all", "purpose": "Warehouse master — location, type (general/cold_storage/hazmat/pharma), capacity"},
                    {"name": "cargo_storage_transaction_all", "purpose": "Storage assignments — warehouse_slot, stored_from, stored_until, temperature_log"},
                    {"name": "cargo_customs_header_all", "purpose": "Customs clearance records — declaration, duty_amount, clearance_officer, status"},
                    {"name": "cargo_customs_life_cycle_all", "purpose": "Customs status audit trail"},
                    {"name": "dangerous_goods_header_all", "purpose": "Dangerous goods declaration — UN number, hazmat_class, packing_group, special_instructions"},
                    {"name": "cargo_flight_assignment_header_all", "purpose": "Shipment-to-flight assignment — may split across multiple flights"},
                    {"name": "cargo_billing_header_all", "purpose": "Cargo billing — weight_charge, fuel_surcharge, customs_fee, total with GST split"},
                ]
            },
            {
                "name": "Gate & Resource Management",
                "description": "Gates, runways, baggage belts, fuel stations, boarding bridges, and resource booking",
                "tables": [
                    {"name": "terminal_header_all", "purpose": "Terminal master — terminal_code, type (domestic/international), capacity, gates_count"},
                    {"name": "gate_header_all", "purpose": "Gate master — gate_number, terminal, type, bridge_available, capacity, current_status"},
                    {"name": "gate_life_cycle_all", "purpose": "Gate status audit trail (available, assigned, maintenance, blocked)"},
                    {"name": "runway_header_all", "purpose": "Runway master — designation, length, width, surface_type, ILS_available, PCN_rating"},
                    {"name": "runway_life_cycle_all", "purpose": "Runway status audit trail (open, maintenance, inspection, closed_weather)"},
                    {"name": "resource_booking_transaction_all", "purpose": "All resource bookings — resource_type, resource_id, booked_from, booked_until, flight_id"},
                    {"name": "gate_assignment_configuration_all", "purpose": "Gate-to-flight assignments with effective_from, effective_to, and airline"},
                    {"name": "runway_assignment_transaction_all", "purpose": "Runway assignment per flight per operation (departure/arrival)"},
                    {"name": "parking_bay_header_all", "purpose": "Aircraft parking bay master — bay_id, type, terminal proximity, max_wingspan"},
                    {"name": "checkin_counter_header_all", "purpose": "Check-in counter master — counter_number, terminal, airline_assigned, current_status"},
                    {"name": "security_lane_header_all", "purpose": "Security screening lane master — lane_number, terminal, type, capacity_per_hour"},
                    {"name": "fuel_station_header_all", "purpose": "Fuel station master — location, fuel_type, flow_rate, current_stock_litres"},
                ]
            },
            {
                "name": "Aircraft Maintenance",
                "description": "Preventive maintenance, work orders, spare parts, inspections, and fault reporting",
                "tables": [
                    {"name": "maintenance_type_header_all", "purpose": "Maintenance type master — A/B/C/D check definitions, interval type, hours/calendar/cycles"},
                    {"name": "maintenance_schedule_header_all", "purpose": "Planned maintenance schedule per aircraft — due_date, due_hours, due_cycles"},
                    {"name": "maintenance_schedule_life_cycle_all", "purpose": "Maintenance schedule status audit trail"},
                    {"name": "work_order_header_all", "purpose": "Maintenance work order master — aircraft, type, priority, grounded_flag, triggered_by"},
                    {"name": "work_order_archive_all", "purpose": "Historical mirror of work_order_header_all"},
                    {"name": "work_order_life_cycle_all", "purpose": "Work order status audit trail (raised→assigned→in_progress→inspected→closed)"},
                    {"name": "work_order_task_details_all", "purpose": "Task breakdown within a work order — task_code, description, estimated_hours, technician"},
                    {"name": "fault_report_header_all", "purpose": "Fault reports — fault_code, severity, grounded_triggered, auto_reassignment_triggered"},
                    {"name": "fault_report_life_cycle_all", "purpose": "Fault status audit trail"},
                    {"name": "spare_part_header_all", "purpose": "Spare parts master — part_number, NSN, description, min_stock, current_stock, storage_location"},
                    {"name": "spare_part_transaction_all", "purpose": "Parts usage and restocking log — issued_for_work_order, quantity, transaction_type"},
                    {"name": "maintenance_engineer_header_all", "purpose": "Maintenance engineers — license_number, type_ratings, certifications, expiry dates"},
                    {"name": "inspection_header_all", "purpose": "Inspection records — inspection_type, inspector, finding_count, pass_fail, next_due"},
                ]
            },
            {
                "name": "Staff & Workforce Management",
                "description": "Airport employees, duty rosters, attendance, leave, and staff scheduling",
                "tables": [
                    {"name": "employee_header_all", "purpose": "Employee master — all airport staff types, department, role, license, clearance_level"},
                    {"name": "employee_archive_all", "purpose": "Historical mirror of employee_header_all"},
                    {"name": "employee_life_cycle_all", "purpose": "Employee status audit trail (active, on_leave, suspended, resigned, terminated)"},
                    {"name": "department_header_all", "purpose": "Department master — operations, security, customs, maintenance, ground_handling, retail"},
                    {"name": "shift_header_all", "purpose": "Shift definitions — shift_name, start_time, end_time, duration_hours, overnight_flag"},
                    {"name": "staff_roster_calendar_all", "purpose": "Monthly duty roster — month_year + day_1..day_31 columns storing shift_code or Off/Leave"},
                    {"name": "attendance_transaction_all", "purpose": "Daily attendance log — punch_in, punch_out, location, late_minutes, overtime_minutes"},
                    {"name": "leave_transaction_all", "purpose": "Leave requests and approvals — type, from_date, to_date, approved_by, status"},
                    {"name": "staff_certification_header_all", "purpose": "Staff certifications — cert_type, cert_number, issued_on, expiry_on, issuing_authority"},
                ]
            },
            {
                "name": "Ground Operations",
                "description": "Ground handling agencies, vehicles, fuelling, catering, and tarmac operations",
                "tables": [
                    {"name": "ground_agency_header_all", "purpose": "Ground handling agency master — name, license, services offered, terminal coverage"},
                    {"name": "ground_agency_archive_all", "purpose": "Historical mirror of ground_agency_header_all"},
                    {"name": "ground_vehicle_header_all", "purpose": "Ground vehicle master — vehicle_type, registration, capacity, current_status, assigned_terminal"},
                    {"name": "ground_vehicle_life_cycle_all", "purpose": "Vehicle status audit trail (available, in_use, maintenance, decommissioned)"},
                    {"name": "fuelling_transaction_all", "purpose": "Aircraft fuelling log — flight_id, fuel_type, quantity_litres, supplier, fuelling_agent, cost"},
                    {"name": "catering_transaction_all", "purpose": "Catering service log — flight_id, airline, meal_count_by_class, caterer, loaded_time"},
                    {"name": "tow_transaction_all", "purpose": "Aircraft towing events — towed_from, towed_to, tow_reason, tug_id, crew_ids"},
                    {"name": "ground_service_transaction_all", "purpose": "All other ground services — cleaning, de-icing, marshalling, GPU connection per flight"},
                ]
            },
            {
                "name": "Terminal & Asset Management",
                "description": "Airport assets, infrastructure, retail, lounges, and asset lifecycle",
                "tables": [
                    {"name": "asset_header_all", "purpose": "Airport asset master — asset_type, location, manufacturer, model, serial_number, purchase_date"},
                    {"name": "asset_archive_all", "purpose": "Historical mirror of asset_header_all"},
                    {"name": "asset_life_cycle_all", "purpose": "Asset status audit trail (operational, maintenance, faulty, decommissioned)"},
                    {"name": "asset_maintenance_transaction_all", "purpose": "Asset maintenance log — maintenance_type, technician, cost, next_due_date"},
                    {"name": "retail_outlet_header_all", "purpose": "Retail and F&B outlet master — outlet_name, terminal, category, lease_area_sqm"},
                    {"name": "retail_lease_header_all", "purpose": "Retail space lease agreements — lessee, area_sqm, monthly_rent, lease_from, lease_until"},
                    {"name": "lounge_header_all", "purpose": "Lounge master — lounge_name, terminal, operator, capacity, access_tiers"},
                    {"name": "lounge_configuration_all", "purpose": "Lounge access rules — eligible_booking_classes, eligible_cards, per_airline_config"},
                    {"name": "parking_header_all", "purpose": "Vehicle parking zone master — zone, level, total_bays, current_occupancy_count, type"},
                    {"name": "parking_transaction_all", "purpose": "Parking entry/exit log — vehicle_registration, zone, entry_time, exit_time, fee"},
                ]
            },
            {
                "name": "Security & Immigration",
                "description": "Security officers, screening, customs declarations, immigration clearance, and incidents",
                "tables": [
                    {"name": "security_screening_transaction_all", "purpose": "Passenger/baggage screening events — lane, officer, result, alert_triggered, timestamp"},
                    {"name": "immigration_clearance_transaction_all", "purpose": "Immigration check log — passenger, officer, visa_type, clearance_result, biometric_matched"},
                    {"name": "customs_declaration_header_all", "purpose": "Customs declarations — passenger, declared_items_json, total_value, duty_amount, status"},
                    {"name": "customs_declaration_life_cycle_all", "purpose": "Customs declaration review status audit trail"},
                    {"name": "incident_header_all", "purpose": "Security/operational incident master — type, severity, location, involved_parties"},
                    {"name": "incident_archive_all", "purpose": "Historical mirror of incident_header_all"},
                    {"name": "incident_life_cycle_all", "purpose": "Incident status audit trail (reported→assigned→investigating→resolved→closed)"},
                    {"name": "incident_response_transaction_all", "purpose": "Response actions per incident — responder, action_taken, timestamp, outcome"},
                    {"name": "watchlist_header_all", "purpose": "Flagged passengers or entities — flag_type, reason, added_by, valid_until"},
                    {"name": "security_alert_transaction_all", "purpose": "Real-time security alerts — source, type, location, escalated_to, resolved_by"},
                ]
            },
            {
                "name": "Finance & Revenue",
                "description": "Airport fees, airline billing, retail rentals, cargo charges, refunds, and financial ledger",
                "tables": [
                    {"name": "airport_revenue_ledger_transaction_all", "purpose": "Master revenue ledger — every credit/debit with closing_balance per terminal"},
                    {"name": "airline_billing_header_all", "purpose": "Airline billing cycle — landing fees, parking charges, ground handling, runway usage"},
                    {"name": "airline_billing_archive_all", "purpose": "Historical mirror of airline_billing_header_all"},
                    {"name": "airline_billing_life_cycle_all", "purpose": "Billing status audit trail (draft→issued→disputed→settled/written_off)"},
                    {"name": "flight_charge_transaction_all", "purpose": "Per-flight charges — landing_fee, parking_fee, ground_handling_fee, ATC_fee with GST split"},
                    {"name": "cargo_revenue_transaction_all", "purpose": "Cargo revenue events — shipment, charge_type, amount, sgst_amount, cgst_amount"},
                    {"name": "retail_revenue_transaction_all", "purpose": "Retail rental payments — lessee, period, rent_amount, GST, closing_balance"},
                    {"name": "refund_header_all", "purpose": "Refund requests — passenger/airline, reason, amount, approved_by, processed_date"},
                    {"name": "refund_life_cycle_all", "purpose": "Refund status audit trail"},
                    {"name": "procurement_header_all", "purpose": "Procurement requests — item, vendor, quantity, estimated_cost, approval chain"},
                    {"name": "procurement_life_cycle_all", "purpose": "Procurement status audit trail (requested→approved→ordered→received→invoiced)"},
                    {"name": "vendor_payment_transaction_all", "purpose": "Vendor payments — vendor, invoice, amount, payment_mode, bank_ref, closing_balance"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "Flight alerts, passenger notifications, staff alerts, and communication log",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue master — recipient, type, channel, delivery_status, priority"},
                    {"name": "notification_archive_all", "purpose": "Historical mirror of notification_header_all"},
                    {"name": "notification_life_cycle_all", "purpose": "Notification delivery status audit trail"},
                    {"name": "notification_template_header_all", "purpose": "Message templates — event_type, channel, language, subject, body with placeholders"},
                    {"name": "flight_alert_transaction_all", "purpose": "Flight-specific alerts — delay, cancellation, gate_change, boarding_call events"},
                    {"name": "communication_log_transaction_all", "purpose": "All outbound communications — SMS, email, push, FIDS board updates with timestamp"},
                ]
            },
            {
                "name": "Audit & Compliance",
                "description": "System audit trails, regulatory compliance, safety reports, and access logs",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All platform actions — who, what, which_entity, before_value, after_value, timestamp"},
                    {"name": "regulatory_report_header_all", "purpose": "Regulatory compliance reports — DGCA, BCAS, customs — report_type, period, submitted_on"},
                    {"name": "safety_report_header_all", "purpose": "Safety occurrence reports — ASR type, severity, contributing_factors, corrective_action"},
                    {"name": "safety_report_life_cycle_all", "purpose": "Safety report investigation status audit trail"},
                    {"name": "data_access_log_transaction_all", "purpose": "PII and sensitive data access log — user, entity, data_type, purpose, timestamp"},
                    {"name": "api_error_log_all", "purpose": "API error tracking — endpoint, error_code, payload_hash, user_id, timestamp"},
                    {"name": "document_header_all", "purpose": "Document management — doc_type, entity_ref, version, uploaded_by, expiry_date"},
                    {"name": "document_life_cycle_all", "purpose": "Document version and approval status audit trail"},
                ]
            },
        ]
    }


def _ecommerce_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    return {
        "project_name": "Multi-Vendor E-Commerce & Logistics Platform",
        "description": desc,
        "domain": "e_commerce",
        "gst_required": gst_required,
        "scale": scale,
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System-wide registry, feature flags, and configuration",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised business ID registry across all entities"},
                    {"name": "factory_reset_all", "purpose": "Global platform feature flags and kill-switches"},
                    {"name": "table_inside_table_all", "purpose": "Table capacity monitoring and alerting"},
                    {"name": "app_version_configuration_all", "purpose": "App version control and force-update rules"},
                    {"name": "platform_configuration_all", "purpose": "Platform-wide runtime configuration"},
                ]
            },
            {
                "name": "User & Identity Management",
                "description": "All user types, authentication, KYC, and access control",
                "tables": [
                    {"name": "user_header_all", "purpose": "All platform users — customers, vendors, admins, partners"},
                    {"name": "user_archive_all", "purpose": "Historical mirror of user_header_all"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit trail (active, suspended, banned)"},
                    {"name": "role_header_all", "purpose": "User roles and permission groups"},
                    {"name": "permission_header_all", "purpose": "Granular feature-level permissions"},
                    {"name": "user_role_transaction_all", "purpose": "Role assignment and revocation history"},
                    {"name": "otp_transaction_all", "purpose": "OTP generation and verification with rate limiting"},
                    {"name": "session_header_all", "purpose": "Active user sessions and device tokens"},
                ]
            },
            {
                "name": "Vendor & KYC Management",
                "description": "Vendor onboarding, KYC verification, brands, and store management",
                "tables": [
                    {"name": "vendor_header_all", "purpose": "Vendor master records — business profile"},
                    {"name": "vendor_archive_all", "purpose": "Historical mirror of vendor_header_all"},
                    {"name": "vendor_life_cycle_all", "purpose": "Vendor status audit trail (pending, verified, suspended)"},
                    {"name": "vendor_kyc_header_all", "purpose": "KYC document submissions and verification status"},
                    {"name": "vendor_kyc_life_cycle_all", "purpose": "KYC verification stage transitions"},
                    {"name": "vendor_brand_header_all", "purpose": "Brands operated by a vendor"},
                    {"name": "vendor_store_header_all", "purpose": "Virtual stores per vendor/brand"},
                    {"name": "vendor_store_configuration_all", "purpose": "Per-store configuration and policies"},
                    {"name": "vendor_bank_header_all", "purpose": "Vendor bank accounts for settlements"},
                ]
            },
            {
                "name": "Product Catalog",
                "description": "Products, categories, variants, bundles, pricing, and digital assets",
                "tables": [
                    {"name": "product_header_all", "purpose": "Product master record"},
                    {"name": "product_archive_all", "purpose": "Historical mirror of product_header_all"},
                    {"name": "product_life_cycle_all", "purpose": "Product status audit trail"},
                    {"name": "product_category_header_all", "purpose": "Product categories and subcategories (tree structure)"},
                    {"name": "product_variant_header_all", "purpose": "Product variants (size, colour, model)"},
                    {"name": "product_attribute_header_all", "purpose": "Product attribute definitions"},
                    {"name": "product_attribute_value_header_all", "purpose": "Attribute values per product/variant"},
                    {"name": "product_pricing_header_all", "purpose": "Active and historical pricing per vendor"},
                    {"name": "product_pricing_life_cycle_all", "purpose": "Price change audit trail"},
                    {"name": "product_bundle_header_all", "purpose": "Product bundle definitions"},
                    {"name": "product_digital_asset_header_all", "purpose": "Images, videos, spec sheets per product"},
                ]
            },
            {
                "name": "Inventory & Warehouse Management",
                "description": "Stock, warehouse operations, transfers, and audit trails",
                "tables": [
                    {"name": "warehouse_header_all", "purpose": "Warehouse master — location, capacity, type"},
                    {"name": "warehouse_archive_all", "purpose": "Historical mirror of warehouse_header_all"},
                    {"name": "warehouse_life_cycle_all", "purpose": "Warehouse status audit trail"},
                    {"name": "inventory_header_all", "purpose": "Current stock level per product per warehouse"},
                    {"name": "inventory_archive_all", "purpose": "Historical inventory snapshots"},
                    {"name": "inventory_transaction_all", "purpose": "Every stock movement — receipt, pick, pack, transfer, return"},
                    {"name": "inventory_audit_header_all", "purpose": "Physical audit and cycle count records"},
                    {"name": "inventory_replenishment_header_all", "purpose": "Replenishment orders and reorder rules"},
                    {"name": "damaged_inventory_header_all", "purpose": "Damaged and quarantined stock tracking"},
                ]
            },
            {
                "name": "Customer Management",
                "description": "Customer profiles, addresses, wishlists, reviews, and loyalty",
                "tables": [
                    {"name": "customer_header_all", "purpose": "Customer profile master"},
                    {"name": "customer_archive_all", "purpose": "Historical mirror of customer_header_all"},
                    {"name": "customer_life_cycle_all", "purpose": "Customer status audit trail"},
                    {"name": "customer_address_header_all", "purpose": "Saved delivery and billing addresses"},
                    {"name": "customer_wishlist_header_all", "purpose": "Wishlisted products per customer"},
                    {"name": "customer_review_header_all", "purpose": "Product and vendor reviews"},
                    {"name": "customer_review_life_cycle_all", "purpose": "Review moderation status audit"},
                    {"name": "membership_header_all", "purpose": "Customer membership tiers and subscriptions"},
                    {"name": "loyalty_point_transaction_all", "purpose": "Reward points earned and redeemed"},
                ]
            },
            {
                "name": "Order Management",
                "description": "Cart, checkout, fulfillment, returns, replacements, and disputes",
                "tables": [
                    {"name": "cart_header_all", "purpose": "Active customer cart sessions"},
                    {"name": "cart_item_header_all", "purpose": "Line items in a cart"},
                    {"name": "order_header_all", "purpose": "Order master — may contain items from multiple vendors"},
                    {"name": "order_archive_all", "purpose": "Historical mirror of order_header_all"},
                    {"name": "order_life_cycle_all", "purpose": "Order status audit trail"},
                    {"name": "order_item_header_all", "purpose": "Individual line items per order per vendor"},
                    {"name": "order_item_life_cycle_all", "purpose": "Line item status audit trail"},
                    {"name": "order_return_header_all", "purpose": "Return requests master"},
                    {"name": "order_return_life_cycle_all", "purpose": "Return request status audit"},
                    {"name": "order_dispute_header_all", "purpose": "Dispute cases for orders"},
                    {"name": "order_cancellation_transaction_all", "purpose": "Cancellation records and reasons"},
                ]
            },
            {
                "name": "Logistics & Delivery",
                "description": "Couriers, shipments, tracking, delivery partners, and reverse logistics",
                "tables": [
                    {"name": "courier_partner_header_all", "purpose": "Courier company master records"},
                    {"name": "delivery_partner_header_all", "purpose": "Individual delivery agents"},
                    {"name": "delivery_partner_life_cycle_all", "purpose": "Delivery partner status audit trail"},
                    {"name": "shipment_header_all", "purpose": "Shipment master — one order may have multiple shipments"},
                    {"name": "shipment_archive_all", "purpose": "Historical mirror of shipment_header_all"},
                    {"name": "shipment_life_cycle_all", "purpose": "Shipment status audit trail"},
                    {"name": "shipment_tracking_transaction_all", "purpose": "Real-time tracking events per shipment"},
                    {"name": "delivery_attempt_transaction_all", "purpose": "Delivery attempt log with POD"},
                    {"name": "reverse_logistics_header_all", "purpose": "Return pickup and inward shipment records"},
                    {"name": "delivery_zone_configuration_all", "purpose": "Serviceability zones per courier partner"},
                ]
            },
            {
                "name": "Finance & Settlements",
                "description": "Commissions, vendor settlements, refunds, wallet, and financial audit",
                "tables": [
                    {"name": "financial_ledger_header_all", "purpose": "Platform-wide financial ledger"},
                    {"name": "financial_ledger_archive_all", "purpose": "Historical mirror of financial_ledger_header_all"},
                    {"name": "financial_ledger_transaction_all", "purpose": "Every financial event (credit, debit, reversal)"},
                    {"name": "vendor_settlement_header_all", "purpose": "Periodic vendor settlement batches"},
                    {"name": "vendor_settlement_life_cycle_all", "purpose": "Settlement status audit trail"},
                    {"name": "commission_transaction_all", "purpose": "Platform commission per order item"},
                    {"name": "refund_header_all", "purpose": "Refund requests and disbursements"},
                    {"name": "refund_life_cycle_all", "purpose": "Refund status audit trail"},
                    {"name": "wallet_header_all", "purpose": "Customer and vendor wallet balances"},
                    {"name": "wallet_transaction_all", "purpose": "All wallet credits and debits"},
                    {"name": "payment_gateway_transaction_all", "purpose": "Payment gateway settlement records"},
                    {"name": "tax_transaction_all", "purpose": "GST and tax calculation per order"},
                ]
            },
            {
                "name": "Marketing & Promotions",
                "description": "Campaigns, coupons, banners, referrals, affiliates, and personalisation",
                "tables": [
                    {"name": "campaign_header_all", "purpose": "Marketing campaign master"},
                    {"name": "campaign_archive_all", "purpose": "Historical mirror of campaign_header_all"},
                    {"name": "campaign_life_cycle_all", "purpose": "Campaign status audit trail"},
                    {"name": "coupon_header_all", "purpose": "Coupon codes and discount rules"},
                    {"name": "coupon_redemption_transaction_all", "purpose": "Coupon usage log per customer per order"},
                    {"name": "banner_header_all", "purpose": "Promotional banners and placements"},
                    {"name": "referral_header_all", "purpose": "Customer referral links and tracking"},
                    {"name": "affiliate_header_all", "purpose": "Affiliate partner records"},
                    {"name": "affiliate_transaction_all", "purpose": "Affiliate commission events"},
                    {"name": "gift_card_header_all", "purpose": "Gift card issuance and balances"},
                    {"name": "gift_card_transaction_all", "purpose": "Gift card usage and recharge history"},
                ]
            },
            {
                "name": "Customer Support",
                "description": "Tickets, complaints, escalations, SLAs, and resolution workflows",
                "tables": [
                    {"name": "support_ticket_header_all", "purpose": "Support case master"},
                    {"name": "support_ticket_archive_all", "purpose": "Historical mirror of support_ticket_header_all"},
                    {"name": "support_ticket_life_cycle_all", "purpose": "Ticket status audit trail"},
                    {"name": "support_escalation_header_all", "purpose": "Escalation records"},
                    {"name": "support_escalation_life_cycle_all", "purpose": "Escalation status audit trail"},
                    {"name": "support_communication_transaction_all", "purpose": "All customer-agent communication logs"},
                    {"name": "support_sla_configuration_all", "purpose": "SLA policy definitions per ticket type"},
                    {"name": "compensation_transaction_all", "purpose": "Compensation credits issued via support"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "Push, SMS, email, and in-app notifications",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue and delivery log"},
                    {"name": "notification_archive_all", "purpose": "Historical mirror of notification_header_all"},
                    {"name": "notification_life_cycle_all", "purpose": "Notification delivery status audit"},
                    {"name": "notification_template_header_all", "purpose": "Message templates per channel and event type"},
                    {"name": "notification_subscription_header_all", "purpose": "Customer notification preferences"},
                    {"name": "communication_log_transaction_all", "purpose": "All outbound communication records (SMS, email, push)"},
                ]
            },
            {
                "name": "Configuration Management",
                "description": "Runtime configuration, approval workflows, and platform policies",
                "tables": [
                    {"name": "configuration_header_all", "purpose": "Configuration record master"},
                    {"name": "configuration_archive_all", "purpose": "Historical mirror of configuration_header_all"},
                    {"name": "configuration_life_cycle_all", "purpose": "Configuration change audit trail"},
                    {"name": "configuration_setting_all", "purpose": "Key-value configuration settings"},
                    {"name": "approval_workflow_header_all", "purpose": "Configurable approval workflow definitions"},
                    {"name": "approval_step_header_all", "purpose": "Individual steps within approval workflows"},
                    {"name": "approval_transaction_all", "purpose": "Approval decisions and escalation log"},
                ]
            },
            {
                "name": "Audit & Compliance",
                "description": "System-wide audit trails, fraud detection, and security monitoring",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All system actions — who did what and when"},
                    {"name": "security_event_header_all", "purpose": "Security incidents and breach attempts"},
                    {"name": "fraud_flag_header_all", "purpose": "Fraud detection alerts per entity or transaction"},
                    {"name": "fraud_flag_life_cycle_all", "purpose": "Fraud case investigation status"},
                    {"name": "blacklist_header_all", "purpose": "Blocked users, devices, IPs, and vendors"},
                    {"name": "api_error_log_all", "purpose": "API error tracking for debugging and alerting"},
                    {"name": "data_access_log_transaction_all", "purpose": "Sensitive data access audit (PII, financial)"},
                ]
            },
            {
                "name": "Analytics & Reporting",
                "description": "Events, metrics, dashboards, and KPI tracking",
                "tables": [
                    {"name": "analytics_event_transaction_all", "purpose": "Raw clickstream and behavioural events"},
                    {"name": "analytics_metric_header_all", "purpose": "Pre-aggregated KPI metrics"},
                    {"name": "analytics_report_header_all", "purpose": "Scheduled and on-demand report definitions"},
                    {"name": "analytics_dashboard_configuration_all", "purpose": "Dashboard layout and widget configuration"},
                    {"name": "vendor_analytics_transaction_all", "purpose": "Vendor-facing sales and performance metrics"},
                ]
            },
        ]
    }


def _healthcare_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    return {
        "project_name": "Healthcare Management Ecosystem",
        "description": desc,
        "domain": "healthcare",
        "gst_required": gst_required,
        "scale": scale,
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System-wide registry and configuration",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised ID registry"},
                    {"name": "factory_reset_all", "purpose": "Platform feature flags"},
                    {"name": "table_inside_table_all", "purpose": "Table capacity monitoring"},
                    {"name": "app_version_configuration_all", "purpose": "App version control"},
                    {"name": "platform_configuration_all", "purpose": "Platform-wide runtime settings"},
                ]
            },
            {
                "name": "User & Identity Management",
                "description": "All user types, roles, authentication, and access control",
                "tables": [
                    {"name": "user_header_all", "purpose": "All system users"},
                    {"name": "user_archive_all", "purpose": "User history mirror"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit trail"},
                    {"name": "role_header_all", "purpose": "User roles"},
                    {"name": "permission_header_all", "purpose": "Granular permissions"},
                    {"name": "otp_transaction_all", "purpose": "OTP verification"},
                    {"name": "session_header_all", "purpose": "Active sessions"},
                ]
            },
            {
                "name": "Patient Management",
                "description": "Patient profiles, demographics, and medical history",
                "tables": [
                    {"name": "patient_header_all", "purpose": "Patient master"},
                    {"name": "patient_archive_all", "purpose": "Patient history mirror"},
                    {"name": "patient_life_cycle_all", "purpose": "Patient status audit"},
                    {"name": "patient_address_header_all", "purpose": "Patient addresses"},
                    {"name": "patient_next_of_kin_header_all", "purpose": "Emergency contacts"},
                    {"name": "patient_insurance_header_all", "purpose": "Insurance policies per patient"},
                ]
            },
            {
                "name": "Doctor & Staff Management",
                "description": "Doctors, nurses, departments, and specialisations",
                "tables": [
                    {"name": "doctor_header_all", "purpose": "Doctor master"},
                    {"name": "doctor_archive_all", "purpose": "Doctor history mirror"},
                    {"name": "doctor_life_cycle_all", "purpose": "Doctor status audit"},
                    {"name": "department_header_all", "purpose": "Hospital departments"},
                    {"name": "staff_header_all", "purpose": "Non-clinical staff"},
                    {"name": "staff_schedule_header_all", "purpose": "Staff duty rosters"},
                ]
            },
            {
                "name": "Appointment & Scheduling",
                "description": "Appointments, slots, queues, and teleconsultations",
                "tables": [
                    {"name": "appointment_header_all", "purpose": "Appointment master"},
                    {"name": "appointment_archive_all", "purpose": "Appointment history"},
                    {"name": "appointment_life_cycle_all", "purpose": "Appointment status audit"},
                    {"name": "slot_configuration_all", "purpose": "Doctor availability slots"},
                    {"name": "queue_header_all", "purpose": "Real-time OPD queue"},
                    {"name": "teleconsultation_header_all", "purpose": "Video/phone consultation sessions"},
                ]
            },
            {
                "name": "Clinical & Medical Records",
                "description": "Diagnoses, prescriptions, vitals, and clinical notes",
                "tables": [
                    {"name": "medical_record_header_all", "purpose": "Patient encounter records"},
                    {"name": "medical_record_archive_all", "purpose": "Medical record history"},
                    {"name": "diagnosis_transaction_all", "purpose": "Diagnosis per encounter"},
                    {"name": "prescription_header_all", "purpose": "Prescriptions issued"},
                    {"name": "prescription_life_cycle_all", "purpose": "Prescription dispensing status"},
                    {"name": "vital_transaction_all", "purpose": "Patient vitals (BP, temp, SpO2)"},
                    {"name": "clinical_note_header_all", "purpose": "Doctor SOAP notes"},
                ]
            },
            {
                "name": "Laboratory Management",
                "description": "Lab tests, samples, results, and quality control",
                "tables": [
                    {"name": "lab_test_header_all", "purpose": "Lab test catalogue"},
                    {"name": "lab_order_header_all", "purpose": "Lab test orders per patient"},
                    {"name": "lab_order_life_cycle_all", "purpose": "Lab order status audit"},
                    {"name": "lab_sample_transaction_all", "purpose": "Sample collection and tracking"},
                    {"name": "lab_result_header_all", "purpose": "Test results and reference ranges"},
                    {"name": "lab_result_life_cycle_all", "purpose": "Result verification and release status"},
                ]
            },
            {
                "name": "Pharmacy Management",
                "description": "Medicines, stock, dispensing, and supplier management",
                "tables": [
                    {"name": "medicine_header_all", "purpose": "Medicine master catalogue"},
                    {"name": "pharmacy_inventory_header_all", "purpose": "Current stock per medicine per location"},
                    {"name": "pharmacy_inventory_transaction_all", "purpose": "Stock movements — purchase, dispense, return"},
                    {"name": "dispense_transaction_all", "purpose": "Medicines dispensed per prescription"},
                    {"name": "supplier_header_all", "purpose": "Medicine suppliers"},
                    {"name": "purchase_order_header_all", "purpose": "Pharmacy purchase orders"},
                ]
            },
            {
                "name": "Billing & Finance",
                "description": "OPD/IPD billing, insurance claims, refunds, and settlements",
                "tables": [
                    {"name": "bill_header_all", "purpose": "Patient bill master"},
                    {"name": "bill_archive_all", "purpose": "Bill history mirror"},
                    {"name": "bill_life_cycle_all", "purpose": "Bill status audit"},
                    {"name": "bill_item_transaction_all", "purpose": "Individual billing line items"},
                    {"name": "payment_transaction_all", "purpose": "Payments received"},
                    {"name": "insurance_claim_header_all", "purpose": "Insurance claim submissions"},
                    {"name": "insurance_claim_life_cycle_all", "purpose": "Claim status audit"},
                    {"name": "refund_transaction_all", "purpose": "Refunds issued"},
                ]
            },
            {
                "name": "Inpatient (IPD) Management",
                "description": "Admissions, wards, beds, care plans, and discharge",
                "tables": [
                    {"name": "admission_header_all", "purpose": "IPD admission master"},
                    {"name": "admission_archive_all", "purpose": "Admission history"},
                    {"name": "admission_life_cycle_all", "purpose": "Admission status audit"},
                    {"name": "ward_header_all", "purpose": "Wards and bed configurations"},
                    {"name": "bed_header_all", "purpose": "Individual bed records"},
                    {"name": "care_plan_header_all", "purpose": "Patient care plans"},
                    {"name": "discharge_summary_header_all", "purpose": "Discharge summaries"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "Appointment reminders, result notifications, and alerts",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue and log"},
                    {"name": "notification_archive_all", "purpose": "Notification history"},
                    {"name": "notification_template_header_all", "purpose": "Message templates"},
                    {"name": "notification_subscription_header_all", "purpose": "Notification preferences"},
                    {"name": "communication_log_transaction_all", "purpose": "All outbound communication records"},
                ]
            },
            {
                "name": "Audit & Compliance",
                "description": "Clinical audit trails and regulatory compliance",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All system actions log"},
                    {"name": "security_event_header_all", "purpose": "Security incidents"},
                    {"name": "data_access_log_transaction_all", "purpose": "PHI access audit trail"},
                    {"name": "api_error_log_all", "purpose": "API errors"},
                ]
            },
            {
                "name": "Configuration Management",
                "description": "Runtime configuration, workflows, and policies",
                "tables": [
                    {"name": "configuration_header_all", "purpose": "Configuration master"},
                    {"name": "configuration_archive_all", "purpose": "Configuration history"},
                    {"name": "configuration_setting_all", "purpose": "Key-value settings"},
                    {"name": "approval_workflow_header_all", "purpose": "Approval workflow definitions"},
                    {"name": "approval_transaction_all", "purpose": "Approval decision log"},
                ]
            },
        ]
    }


def _logistics_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    return {
        "project_name": "Logistics & Supply Chain Platform",
        "description": desc,
        "domain": "logistics",
        "gst_required": gst_required,
        "scale": scale,
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System-wide registry and platform configuration",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised ID registry"},
                    {"name": "factory_reset_all", "purpose": "Platform feature flags"},
                    {"name": "table_inside_table_all", "purpose": "Table capacity monitoring"},
                    {"name": "app_version_configuration_all", "purpose": "App version control"},
                ]
            },
            {
                "name": "User & Identity Management",
                "description": "All users, roles, and authentication",
                "tables": [
                    {"name": "user_header_all", "purpose": "All system users"},
                    {"name": "user_archive_all", "purpose": "User history mirror"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit"},
                    {"name": "role_header_all", "purpose": "User roles"},
                    {"name": "otp_transaction_all", "purpose": "OTP verification"},
                    {"name": "session_header_all", "purpose": "Active sessions"},
                ]
            },
            {
                "name": "Fleet & Vehicle Management",
                "description": "Vehicles, maintenance, documents, and compliance",
                "tables": [
                    {"name": "vehicle_header_all", "purpose": "Vehicle master"},
                    {"name": "vehicle_archive_all", "purpose": "Vehicle history mirror"},
                    {"name": "vehicle_life_cycle_all", "purpose": "Vehicle status audit"},
                    {"name": "vehicle_document_header_all", "purpose": "Vehicle documents (RC, insurance, PUC)"},
                    {"name": "maintenance_header_all", "purpose": "Maintenance and service records"},
                    {"name": "fuel_transaction_all", "purpose": "Fuel consumption log"},
                ]
            },
            {
                "name": "Driver Management",
                "description": "Drivers, documents, payouts, attendance, and ratings",
                "tables": [
                    {"name": "driver_header_all", "purpose": "Driver master"},
                    {"name": "driver_archive_all", "purpose": "Driver history mirror"},
                    {"name": "driver_life_cycle_all", "purpose": "Driver status audit"},
                    {"name": "driver_document_header_all", "purpose": "Driver documents (licence, Aadhaar)"},
                    {"name": "driver_attendance_transaction_all", "purpose": "Daily attendance log"},
                    {"name": "driver_rating_transaction_all", "purpose": "Delivery ratings from customers"},
                    {"name": "driver_payout_header_all", "purpose": "Driver earnings and incentives"},
                ]
            },
            {
                "name": "Shipment & Order Management",
                "description": "Shipments, manifests, and order processing",
                "tables": [
                    {"name": "shipment_header_all", "purpose": "Shipment master"},
                    {"name": "shipment_archive_all", "purpose": "Shipment history mirror"},
                    {"name": "shipment_life_cycle_all", "purpose": "Shipment status audit"},
                    {"name": "shipment_item_transaction_all", "purpose": "Items within a shipment"},
                    {"name": "manifest_header_all", "purpose": "Route manifests per vehicle per day"},
                    {"name": "delivery_attempt_transaction_all", "purpose": "Delivery attempts with POD"},
                    {"name": "reverse_logistics_header_all", "purpose": "Return pickup records"},
                ]
            },
            {
                "name": "Route & Zone Management",
                "description": "Routes, zones, and serviceability configuration",
                "tables": [
                    {"name": "route_header_all", "purpose": "Route master"},
                    {"name": "zone_header_all", "purpose": "Delivery zone definitions"},
                    {"name": "zone_configuration_all", "purpose": "Zone-level delivery rules"},
                    {"name": "route_optimisation_transaction_all", "purpose": "Route optimisation results"},
                    {"name": "pickup_schedule_header_all", "purpose": "Pickup slot scheduling"},
                ]
            },
            {
                "name": "Warehouse & Inventory",
                "description": "Warehouses, stock, movements, and audits",
                "tables": [
                    {"name": "warehouse_header_all", "purpose": "Warehouse master"},
                    {"name": "warehouse_archive_all", "purpose": "Warehouse history mirror"},
                    {"name": "inventory_header_all", "purpose": "Current stock levels"},
                    {"name": "inventory_transaction_all", "purpose": "All stock movements"},
                    {"name": "inventory_audit_header_all", "purpose": "Physical stock audits"},
                ]
            },
            {
                "name": "Customer & Partner Management",
                "description": "Clients, courier partners, and SLA management",
                "tables": [
                    {"name": "customer_header_all", "purpose": "Business client master"},
                    {"name": "customer_archive_all", "purpose": "Customer history mirror"},
                    {"name": "courier_partner_header_all", "purpose": "Courier partner master"},
                    {"name": "partner_sla_configuration_all", "purpose": "SLA definitions per partner"},
                    {"name": "partner_performance_transaction_all", "purpose": "Partner performance metrics"},
                ]
            },
            {
                "name": "Finance & Billing",
                "description": "Invoicing, collections, driver payouts, and settlements",
                "tables": [
                    {"name": "invoice_header_all", "purpose": "Client invoices"},
                    {"name": "invoice_archive_all", "purpose": "Invoice history mirror"},
                    {"name": "invoice_life_cycle_all", "purpose": "Invoice status audit"},
                    {"name": "payment_transaction_all", "purpose": "Payment receipts"},
                    {"name": "driver_settlement_header_all", "purpose": "Driver settlement batches"},
                    {"name": "commission_transaction_all", "purpose": "Platform commission records"},
                ]
            },
            {
                "name": "Tracking & IoT",
                "description": "Real-time GPS tracking and device integration",
                "tables": [
                    {"name": "tracking_event_transaction_all", "purpose": "GPS/IoT tracking events"},
                    {"name": "device_header_all", "purpose": "IoT device registry"},
                    {"name": "device_life_cycle_all", "purpose": "Device status audit"},
                    {"name": "geofence_header_all", "purpose": "Geofence zone definitions"},
                    {"name": "geofence_event_transaction_all", "purpose": "Geofence entry/exit events"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "Shipment alerts, driver updates, and customer notifications",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue and log"},
                    {"name": "notification_archive_all", "purpose": "Notification history"},
                    {"name": "notification_template_header_all", "purpose": "Message templates"},
                    {"name": "communication_log_transaction_all", "purpose": "Outbound communication records"},
                ]
            },
            {
                "name": "Audit & Compliance",
                "description": "System-wide audit and regulatory compliance",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All system actions log"},
                    {"name": "security_event_header_all", "purpose": "Security incidents"},
                    {"name": "api_error_log_all", "purpose": "API errors"},
                    {"name": "data_access_log_transaction_all", "purpose": "Sensitive data access audit"},
                ]
            },
            {
                "name": "Configuration Management",
                "description": "Platform configuration, workflows, and policies",
                "tables": [
                    {"name": "configuration_header_all", "purpose": "Configuration master"},
                    {"name": "configuration_archive_all", "purpose": "Configuration history"},
                    {"name": "configuration_setting_all", "purpose": "Key-value settings"},
                    {"name": "approval_workflow_header_all", "purpose": "Approval workflow definitions"},
                    {"name": "approval_transaction_all", "purpose": "Approval decision log"},
                ]
            },
        ]
    }


def _hr_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    """HR/Payroll domain fallback — 13 modules, 80+ tables."""
    return _generic_enterprise_fallback(desc, "hr", gst_required, scale)


def _finance_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    """Finance/FinTech domain fallback."""
    return _generic_enterprise_fallback(desc, "financial", gst_required, scale)


def _saas_fallback(desc: str, gst_required: bool, scale: str) -> dict:
    """Multi-tenant SaaS domain fallback."""
    return _generic_enterprise_fallback(desc, "multi_tenant_saas", gst_required, scale)


def _generic_enterprise_fallback(desc: str, domain: str, gst_required: bool, scale: str) -> dict:
    """
    Generic but comprehensive fallback — 13 modules, 80+ tables.
    Used for domains that don't have a domain-specific fallback.
    """
    return {
        "project_name": "Enterprise System",
        "description": desc,
        "domain": domain,
        "gst_required": gst_required,
        "scale": scale,
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System-wide registry, feature flags, and configuration",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised business ID registry"},
                    {"name": "factory_reset_all", "purpose": "Global platform feature flags"},
                    {"name": "table_inside_table_all", "purpose": "Table capacity monitoring"},
                    {"name": "app_version_configuration_all", "purpose": "App version control"},
                    {"name": "platform_configuration_all", "purpose": "Platform-wide runtime configuration"},
                ]
            },
            {
                "name": "User & Identity Management",
                "description": "All user types, authentication, and access control",
                "tables": [
                    {"name": "user_header_all", "purpose": "All system users"},
                    {"name": "user_archive_all", "purpose": "User history mirror"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit trail"},
                    {"name": "role_header_all", "purpose": "User roles and permission groups"},
                    {"name": "permission_header_all", "purpose": "Granular feature-level permissions"},
                    {"name": "user_role_transaction_all", "purpose": "Role assignment history"},
                    {"name": "otp_transaction_all", "purpose": "OTP verification with rate limiting"},
                    {"name": "session_header_all", "purpose": "Active user sessions"},
                ]
            },
            {
                "name": "Core Entity A",
                "description": "Primary business entity with full audit trail",
                "tables": [
                    {"name": "entity_header_all", "purpose": "Primary entity master records"},
                    {"name": "entity_archive_all", "purpose": "Historical mirror of entity_header_all"},
                    {"name": "entity_life_cycle_all", "purpose": "Entity status audit trail"},
                    {"name": "entity_details_all", "purpose": "Extended entity attributes"},
                    {"name": "entity_configuration_all", "purpose": "Entity-level settings"},
                    {"name": "entity_document_header_all", "purpose": "Entity document storage"},
                ]
            },
            {
                "name": "Core Entity B",
                "description": "Secondary business entity with full audit trail",
                "tables": [
                    {"name": "sub_entity_header_all", "purpose": "Secondary entity master"},
                    {"name": "sub_entity_archive_all", "purpose": "Historical mirror"},
                    {"name": "sub_entity_life_cycle_all", "purpose": "Status audit trail"},
                    {"name": "sub_entity_transaction_all", "purpose": "Entity event log"},
                    {"name": "sub_entity_configuration_all", "purpose": "Entity-level settings"},
                ]
            },
            {
                "name": "Transaction Management",
                "description": "All operational and financial transactions",
                "tables": [
                    {"name": "transaction_header_all", "purpose": "Transaction master"},
                    {"name": "transaction_archive_all", "purpose": "Transaction history mirror"},
                    {"name": "transaction_life_cycle_all", "purpose": "Transaction status audit"},
                    {"name": "transaction_item_header_all", "purpose": "Line items per transaction"},
                    {"name": "payment_transaction_all", "purpose": "Payment records"},
                    {"name": "payment_failed_transaction_all", "purpose": "Failed payment log"},
                ]
            },
            {
                "name": "Finance & Accounting",
                "description": "Ledger, settlements, refunds, taxes, and financial audit",
                "tables": [
                    {"name": "financial_ledger_header_all", "purpose": "Financial ledger master"},
                    {"name": "financial_ledger_archive_all", "purpose": "Ledger history mirror"},
                    {"name": "financial_ledger_transaction_all", "purpose": "Every financial event"},
                    {"name": "settlement_header_all", "purpose": "Settlement batches"},
                    {"name": "settlement_life_cycle_all", "purpose": "Settlement status audit"},
                    {"name": "refund_header_all", "purpose": "Refund requests"},
                    {"name": "refund_life_cycle_all", "purpose": "Refund status audit"},
                    {"name": "tax_transaction_all", "purpose": "Tax calculation per transaction"},
                ]
            },
            {
                "name": "Workflow & Approval",
                "description": "Configurable multi-step approval workflows",
                "tables": [
                    {"name": "workflow_header_all", "purpose": "Workflow definition master"},
                    {"name": "workflow_archive_all", "purpose": "Workflow history"},
                    {"name": "workflow_step_header_all", "purpose": "Individual workflow steps"},
                    {"name": "approval_transaction_all", "purpose": "Approval decisions and escalations"},
                    {"name": "approval_delegation_header_all", "purpose": "Delegation rules"},
                ]
            },
            {
                "name": "Resource & Inventory Management",
                "description": "Resources, inventory, and asset tracking",
                "tables": [
                    {"name": "resource_header_all", "purpose": "Resource/asset master"},
                    {"name": "resource_archive_all", "purpose": "Resource history mirror"},
                    {"name": "resource_life_cycle_all", "purpose": "Resource status audit"},
                    {"name": "inventory_header_all", "purpose": "Current inventory levels"},
                    {"name": "inventory_transaction_all", "purpose": "All inventory movements"},
                    {"name": "inventory_audit_header_all", "purpose": "Physical audits"},
                ]
            },
            {
                "name": "Reporting & Analytics",
                "description": "Events, metrics, and operational dashboards",
                "tables": [
                    {"name": "analytics_event_transaction_all", "purpose": "Raw behavioural and operational events"},
                    {"name": "analytics_metric_header_all", "purpose": "Pre-aggregated KPIs"},
                    {"name": "analytics_report_header_all", "purpose": "Report definitions"},
                    {"name": "analytics_dashboard_configuration_all", "purpose": "Dashboard configurations"},
                    {"name": "kpi_target_configuration_all", "purpose": "KPI targets per period"},
                ]
            },
            {
                "name": "Customer Support",
                "description": "Tickets, escalations, SLAs, and resolutions",
                "tables": [
                    {"name": "support_ticket_header_all", "purpose": "Support ticket master"},
                    {"name": "support_ticket_archive_all", "purpose": "Ticket history"},
                    {"name": "support_ticket_life_cycle_all", "purpose": "Ticket status audit"},
                    {"name": "support_escalation_header_all", "purpose": "Escalation records"},
                    {"name": "support_communication_transaction_all", "purpose": "Communication logs"},
                    {"name": "sla_configuration_all", "purpose": "SLA policy definitions"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "Push, SMS, email notifications and templates",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue and delivery log"},
                    {"name": "notification_archive_all", "purpose": "Notification history"},
                    {"name": "notification_life_cycle_all", "purpose": "Delivery status audit"},
                    {"name": "notification_template_header_all", "purpose": "Message templates"},
                    {"name": "notification_subscription_header_all", "purpose": "User notification preferences"},
                    {"name": "communication_log_transaction_all", "purpose": "Outbound communication records"},
                ]
            },
            {
                "name": "Audit & Compliance",
                "description": "System-wide audit trails and security monitoring",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All system actions log"},
                    {"name": "security_event_header_all", "purpose": "Security incidents"},
                    {"name": "fraud_flag_header_all", "purpose": "Fraud detection alerts"},
                    {"name": "blacklist_header_all", "purpose": "Blocked users and entities"},
                    {"name": "api_error_log_all", "purpose": "API error tracking"},
                    {"name": "data_access_log_transaction_all", "purpose": "Sensitive data access audit"},
                ]
            },
            {
                "name": "Configuration Management",
                "description": "Runtime settings, policies, and access rules",
                "tables": [
                    {"name": "configuration_header_all", "purpose": "Configuration master"},
                    {"name": "configuration_archive_all", "purpose": "Configuration history"},
                    {"name": "configuration_life_cycle_all", "purpose": "Configuration change audit"},
                    {"name": "configuration_setting_all", "purpose": "Key-value runtime settings"},
                    {"name": "configuration_option_all", "purpose": "Allowed values per setting"},
                    {"name": "configuration_value_all", "purpose": "Active configuration values"},
                ]
            },
        ]
    }