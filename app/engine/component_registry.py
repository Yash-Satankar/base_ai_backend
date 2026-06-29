# app/engine/component_registry.py
"""
Registry of reusable, configurable Architecture Components.
Provides standard, production-grade blueprints for common enterprise engines.
"""

from typing import Dict, List
from app.schemas.blueprint_schema import BlueprintTableSpec, TableType

REUSABLE_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "LedgerEngine": {
        "name": "Ledger Engine",
        "description": "Double-entry financial ledger for tracking balances, transactions, and settlements.",
        "tables": [
            BlueprintTableSpec(
                name="ledger_account_header_all",
                purpose="Stores account balances, currency types, owner references, and current status.",
                table_type=TableType.HEADER,
                entity_name="ledger_account",
                requires_archive=True,
                requires_lifecycle=True
            ),
            BlueprintTableSpec(
                name="ledger_transaction_all",
                purpose="Double-entry transaction log recording debit/credit accounts, amount, reference, and state.",
                table_type=TableType.TRANSACTION,
                entity_name="ledger_transaction"
            ),
            BlueprintTableSpec(
                name="ledger_balance_archive_all",
                purpose="Archived snapshots of ledger balances for financial period closing and audit reconciliation.",
                table_type=TableType.ARCHIVE,
                entity_name="ledger_balance"
            )
        ],
        "sql_guideline": """
-- Standard Ledger Schema Guidelines:
-- ledger_account_header_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, account_number VARCHAR(100) UNIQUE, owner_id VARCHAR(36),
--   - currency VARCHAR(3), current_balance DECIMAL(15,4) DEFAULT 0.0000, status VARCHAR(20)
-- ledger_transaction_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, debit_account_id VARCHAR(36) FK, credit_account_id VARCHAR(36) FK,
--   - amount DECIMAL(15,4), transaction_type VARCHAR(50), reference_id VARCHAR(100), month_year VARCHAR(7)
"""
    },
    "ApprovalEngine": {
        "name": "Approval Engine",
        "description": "Multi-stage, role-based approval workflow tracker.",
        "tables": [
            BlueprintTableSpec(
                name="approval_request_header_all",
                purpose="Tracks the overall status of a document/entity approval request.",
                table_type=TableType.HEADER,
                entity_name="approval_request",
                requires_archive=True,
                requires_lifecycle=True
            ),
            BlueprintTableSpec(
                name="approval_step_transaction_all",
                purpose="Appends individual approval step logs (who approved, role, comments, timestamp).",
                table_type=TableType.TRANSACTION,
                entity_name="approval_step"
            ),
            BlueprintTableSpec(
                name="approval_workflow_configuration_all",
                purpose="Defines multi-stage approval rules and required roles per entity type.",
                table_type=TableType.CONFIGURATION,
                entity_name="approval_workflow_configuration"
            )
        ],
        "sql_guideline": """
-- Standard Approval Schema Guidelines:
-- approval_request_header_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, target_entity_type VARCHAR(100), target_entity_id VARCHAR(36),
--   - current_step_number INT, total_steps INT, status VARCHAR(20)
-- approval_step_transaction_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, approval_request_id VARCHAR(36) FK, step_number INT,
--   - action VARCHAR(20) (APPROVED|REJECTED|DELEGATED), actor_user_id VARCHAR(36), comments TEXT
"""
    },
    "AuditEngine": {
        "name": "Audit Engine",
        "description": "System-wide change data capture and security compliance log.",
        "tables": [
            BlueprintTableSpec(
                name="audit_log_transaction_all",
                purpose="Centralised audit log tracking before/after JSON states of modified database rows.",
                table_type=TableType.TRANSACTION,
                entity_name="audit_log"
            )
        ],
        "sql_guideline": """
-- Standard Audit Schema Guidelines:
-- audit_log_transaction_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, table_name VARCHAR(100), row_id VARCHAR(36),
--   - action VARCHAR(10) (INSERT|UPDATE|DELETE), before_state JSON, after_state JSON,
--   - actor_user_id VARCHAR(36), ip_address VARCHAR(45), user_agent VARCHAR(255)
"""
    },
    "NotificationEngine": {
        "name": "Notification Engine",
        "description": "Asynchronous multi-channel notification dispatcher and queue.",
        "tables": [
            BlueprintTableSpec(
                name="notification_queue_transaction_all",
                purpose="Queue for dispatching SMS, Email, and Push notifications with retry count.",
                table_type=TableType.TRANSACTION,
                entity_name="notification_queue"
            ),
            BlueprintTableSpec(
                name="notification_template_configuration_all",
                purpose="Configurable template strings with placeholders for notifications.",
                table_type=TableType.CONFIGURATION,
                entity_name="notification_template"
            )
        ],
        "sql_guideline": """
-- Standard Notification Schema Guidelines:
-- notification_queue_transaction_all must contain:
--   - id VARCHAR(36) PRIMARY KEY, recipient VARCHAR(255), channel VARCHAR(10) (EMAIL|SMS|PUSH),
--   - subject VARCHAR(255), body_text TEXT, status VARCHAR(20) (PENDING|SENT|FAILED),
--   - retry_count INT DEFAULT 0, last_error_message TEXT, scheduled_at TIMESTAMP
"""
    },
    "RBACEngine": {
        "name": "RBAC Engine",
        "description": "Role-Based Access Control and organization membership manager.",
        "tables": [
            BlueprintTableSpec(
                name="role_header_all",
                purpose="System roles master record.",
                table_type=TableType.HEADER,
                entity_name="role"
            ),
            BlueprintTableSpec(
                name="permission_header_all",
                purpose="System granular permissions master record.",
                table_type=TableType.HEADER,
                entity_name="permission"
            ),
            BlueprintTableSpec(
                name="user_role_junction_all",
                purpose="Junction table mapping users to their assigned roles.",
                table_type=TableType.JUNCTION,
                entity_name="user_role"
            ),
            BlueprintTableSpec(
                name="role_permission_junction_all",
                purpose="Junction table mapping permissions to roles.",
                table_type=TableType.JUNCTION,
                entity_name="role_permission"
            )
        ],
        "sql_guideline": """
-- Standard RBAC Schema Guidelines:
-- role_header_all: id VARCHAR(36) PRIMARY KEY, name VARCHAR(50) UNIQUE, description VARCHAR(255)
-- permission_header_all: id VARCHAR(36) PRIMARY KEY, code VARCHAR(100) UNIQUE, description VARCHAR(255)
-- user_role_junction_all: user_id VARCHAR(36), role_id VARCHAR(36), PRIMARY KEY (user_id, role_id)
-- role_permission_junction_all: role_id VARCHAR(36), permission_id VARCHAR(36), PRIMARY KEY (role_id, permission_id)
"""
    }
}

import typing
from typing import Any
