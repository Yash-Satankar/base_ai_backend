# app/engine/architecture_planner.py

import json
import logging
from app.services.ai_service import generate_schema

logger = logging.getLogger(__name__)


def generate_deep_blueprint(
    requirement: str,
    domain: str,
    gst_required: bool,
    scale: str,
) -> dict:
    """
    Generate a deep, production-grade blueprint.
    Forces 8-12 tables per module, all three layers mandatory.
    """

    system_prompt = f"""You are a senior database architect designing an enterprise system.
Generate a comprehensive database blueprint with DEEP module structure.

RULES:
- Minimum 6 modules
- Each module must have 6-12 tables
- Every entity needs: _header_all + _archive_all + _life_cycle_all
- Transaction entities need: _transaction_all
- Configuration entities need: _configuration_all
- Always include unique_id_header_all in Infrastructure module
- Always include factory_reset_all in Infrastructure module

DOMAIN: {domain.replace('_', ' ').title()}
GST REQUIRED: {gst_required}
SCALE: {scale}

Return ONLY valid JSON. No markdown. No explanation.

Required JSON structure:
{{
  "project_name": "short name",
  "description": "one sentence",
  "domain": "{domain}",
  "gst_required": {str(gst_required).lower()},
  "scale": "{scale}",
  "modules": [
    {{
      "name": "Module Name",
      "description": "what this module handles",
      "tables": [
        {{"name": "entity_header_all", "purpose": "master entity records"}},
        {{"name": "entity_archive_all", "purpose": "historical mirror of entity_header_all"}},
        {{"name": "entity_life_cycle_all", "purpose": "status change audit trail"}},
        {{"name": "entity_transaction_all", "purpose": "event log"}},
        {{"name": "entity_configuration_all", "purpose": "temporal settings"}}
      ]
    }}
  ]
}}"""

    user_prompt = f"""Generate a complete enterprise blueprint for:

{requirement}

Requirements:
- Generate ALL modules needed for a production system
- Every entity must have header + archive + lifecycle tables
- Include infrastructure, audit, notification, and configuration modules
- Think like you are building this for a large enterprise
- Minimum 60 tables total across all modules"""

    try:
        response = generate_schema(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        content = response["content"].strip()

        # Strip markdown
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        data = json.loads(content)
        logger.info(
            f"✅ Deep blueprint: {len(data.get('modules', []))} modules, "
            f"{sum(len(m.get('tables',[])) for m in data.get('modules',[]))} tables"
        )
        return data

    except Exception as e:
        logger.error(f"Deep blueprint generation failed: {e}")
        return _fallback_deep_blueprint(requirement, domain, gst_required)


def _fallback_deep_blueprint(
    requirement: str,
    domain: str,
    gst_required: bool,
) -> dict:
    """
    Hardcoded deep blueprint when AI fails.
    Covers all standard modules every enterprise system needs.
    """
    return {
        "project_name": "Enterprise System",
        "description": requirement[:100],
        "domain": domain,
        "gst_required": gst_required,
        "scale": "medium",
        "modules": [
            {
                "name": "Infrastructure",
                "description": "System-wide registry and configuration",
                "tables": [
                    {"name": "unique_id_header_all", "purpose": "Centralised business ID registry"},
                    {"name": "factory_reset_all", "purpose": "Global platform feature flags"},
                    {"name": "table_inside_table_all", "purpose": "Table capacity monitoring"},
                    {"name": "app_version_configuration_all", "purpose": "App version control"},
                ]
            },
            {
                "name": "User Management",
                "description": "All user types and authentication",
                "tables": [
                    {"name": "user_header_all", "purpose": "All system users"},
                    {"name": "user_archive_all", "purpose": "User history mirror"},
                    {"name": "user_life_cycle_all", "purpose": "User status audit trail"},
                    {"name": "role_header_all", "purpose": "User roles and permissions"},
                    {"name": "user_role_transaction_all", "purpose": "Role assignment history"},
                    {"name": "otp_transaction_all", "purpose": "OTP verification with rate limiting"},
                    {"name": "session_header_all", "purpose": "Active user sessions"},
                ]
            },
            {
                "name": "Core Entity",
                "description": "Primary business entities",
                "tables": [
                    {"name": "entity_header_all", "purpose": "Core entity master"},
                    {"name": "entity_archive_all", "purpose": "Entity history mirror"},
                    {"name": "entity_life_cycle_all", "purpose": "Entity status audit"},
                    {"name": "entity_details_all", "purpose": "Extended entity attributes"},
                    {"name": "entity_configuration_all", "purpose": "Entity-level settings"},
                ]
            },
            {
                "name": "Transaction Management",
                "description": "All financial and operational transactions",
                "tables": [
                    {"name": "transaction_header_all", "purpose": "Transaction master"},
                    {"name": "transaction_archive_all", "purpose": "Transaction history"},
                    {"name": "transaction_life_cycle_all", "purpose": "Transaction status audit"},
                    {"name": "payment_transaction_all", "purpose": "Payment records"},
                    {"name": "payment_failed_all", "purpose": "Failed payment log"},
                ]
            },
            {
                "name": "Notification & Communication",
                "description": "All system notifications and alerts",
                "tables": [
                    {"name": "notification_header_all", "purpose": "Notification queue and log"},
                    {"name": "notification_archive_all", "purpose": "Notification history"},
                    {"name": "alert_header_all", "purpose": "System alerts"},
                    {"name": "email_scheduler_all", "purpose": "Email scheduling queue"},
                ]
            },
            {
                "name": "Audit & Security",
                "description": "System-wide audit trails and security",
                "tables": [
                    {"name": "audit_log_header_all", "purpose": "All system actions log"},
                    {"name": "security_event_header_all", "purpose": "Security incidents"},
                    {"name": "api_error_log_all", "purpose": "API error tracking"},
                    {"name": "blacklist_header_all", "purpose": "Blocked users/entities"},
                ]
            },
        ]
    }