"""Permission catalog and the default role → permission matrix (RBAC). Resource-level rules live in authz.abac.

Deny by default: a permission not listed for a role is not granted. SYSTEM_ADMIN deliberately has no financial
approval permissions; AI_AGENT and SCHEDULER are machine identities with narrowly scoped permissions.
"""

from __future__ import annotations

from finguard.models.enums import RoleName as R

PERMISSIONS: dict[str, str] = {
    "contracts:create": "Create a contract draft and upload documents (own)",
    "contracts:read_own": "Read own contracts, extraction and payment status",
    "contracts:read_org": "Read all contracts in the organization",
    "contracts:correct_fields_own": "Correct extracted fields on own draft contracts",
    "contracts:correct_fields": "Correct extracted fields during validation",
    "contracts:submit_own": "Submit own contract for validation",
    "contracts:validate": "Perform contract validation",
    "contracts:approve": "Approve or reject contracts within approval limits",
    "contracts:suspend": "Suspend / terminate contracts (hold)",
    "amendments:request_own": "Request an amendment on own active contract",
    "amendments:approve": "Approve or reject amendments within approval limits",
    "payments:read_own": "Read own scheduled payments",
    "payments:read_org": "Read organization payments",
    "payments:prepare": "Prepare payment requests and reconcile",
    "payments:approve": "Approve held payments within approval limits",
    "payments:execute_scheduled": "Execute due scheduled payments via governance (machine)",
    "payments:reconcile": "Reconcile unknown provider outcomes",
    "payments:hold": "Place or release compliance holds",
    "invoices:submit_own": "Submit invoices for own scheduled payments",
    "invoices:review": "Accept or reject invoices",
    "beneficiaries:manage_own": "Register own payout beneficiary",
    "beneficiaries:verify": "Verify beneficiaries",
    "approvals:read": "View approval queues",
    "escalations:resolve": "Resolve escalated cases",
    "risk:read": "Read risk assessments and governance decisions",
    "audit:read": "Read audit events",
    "audit:export": "Export audit reports",
    "policies:read": "Read risk/governance policy versions",
    "policies:manage": "Create and activate policy versions",
    "approval_limits:manage": "Configure approval limits",
    "users:manage": "Manage users and role assignments",
    "system:read": "View system health and integration status",
    "agent:use": "Use the AI financial agent",
    "agent:tools": "Invoke allowlisted agent tools (machine identity)",
    "evaluation:read": "View research evaluation runs",
    "evaluation:run": "Run evaluations",
    "reports:read": "View operational reports",
    "financials:read": "Read the organization's financial ledger and ask the agent financial questions",
}

_FINANCE_READ = {"contracts:read_org", "payments:read_org", "approvals:read", "risk:read", "reports:read", "agent:use",
                 "financials:read"}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    R.CONTRACTOR: {"contracts:create", "contracts:read_own", "contracts:correct_fields_own", "contracts:submit_own",
                   "amendments:request_own", "payments:read_own", "invoices:submit_own", "beneficiaries:manage_own",
                   "agent:use"},
    R.FINANCE_ANALYST: _FINANCE_READ | {"contracts:validate", "contracts:correct_fields", "payments:prepare",
                                        "payments:reconcile", "invoices:review", "beneficiaries:verify"},
    R.FINANCE_APPROVER: _FINANCE_READ | {"contracts:approve", "amendments:approve", "payments:approve"},
    R.RISK_OFFICER: _FINANCE_READ | {"escalations:resolve", "payments:hold", "contracts:suspend", "audit:read",
                                     "policies:read", "evaluation:read"},
    R.FINANCE_MANAGER: _FINANCE_READ | {"contracts:approve", "amendments:approve", "payments:approve",
                                        "escalations:resolve", "contracts:suspend", "approval_limits:manage",
                                        "policies:read", "contracts:validate", "evaluation:read"},
    R.AUDITOR: {"contracts:read_org", "payments:read_org", "approvals:read", "risk:read", "audit:read", "audit:export",
                "policies:read", "evaluation:read", "reports:read", "financials:read", "agent:use"},
    R.SYSTEM_ADMIN: {"users:manage", "system:read", "policies:read", "policies:manage", "evaluation:read",
                     "evaluation:run"},
    R.AI_AGENT: {"agent:tools"},
    R.SCHEDULER: {"payments:execute_scheduled", "payments:reconcile"},
}

ROLE_DESCRIPTIONS = {
    R.CONTRACTOR: "External contractor: submits own contracts, invoices and amendment requests",
    R.FINANCE_ANALYST: "Validates contracts, prepares and reconciles payments",
    R.FINANCE_APPROVER: "Approves contracts, amendments and payments within limits",
    R.RISK_OFFICER: "Reviews escalations, holds, policy violations and audit history",
    R.FINANCE_MANAGER: "Higher-limit approvals, escalation resolution, approval-limit configuration",
    R.AUDITOR: "Read-only access to audit, versions, approvals and risk",
    R.SYSTEM_ADMIN: "Technical administration. No financial approval authority",
    R.AI_AGENT: "Machine identity for the AI agent (allowlisted tools only)",
    R.SCHEDULER: "Machine identity for the recurring-payment scheduler",
}

SERVICE_ROLES = {R.AI_AGENT, R.SCHEDULER}

# Roles a SYSTEM_ADMIN may not grant to themselves (separation of technical and financial authority).
FINANCIAL_AUTHORITY_ROLES = {R.FINANCE_APPROVER, R.FINANCE_MANAGER, R.RISK_OFFICER}
