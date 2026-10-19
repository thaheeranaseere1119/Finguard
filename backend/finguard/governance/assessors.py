"""Pure risk-factor scoring and hard-rule evaluation for each governed action.

Services build a *snapshot* from the database; the evaluation harness builds the same snapshot from scenario
specifications. Both call the functions here, so the research results measure exactly the code that runs in
production. Nothing in this module touches the database, the network, or an LLM.

Principles enforced here:
  * good evidence lowers E/U, never F (a valid contract does not make a large payment low-impact);
  * an actor's role is authorization context, never a factor discount;
  * a human clicking "approve" does not lower the risk of the operation;
  * contract approval, amendment approval and payment execution are separate decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finguard.governance.policy import DecisionOutcome, HardRuleResult, decide, rule
from finguard.models.enums import ActionType, ContractStatus, Decision
from finguard.risk.engine import RiskVector, band, make_factor, risk_score


@dataclass
class Assessment:
    action_type: ActionType
    vector: RiskVector
    rules: list[HardRuleResult]
    score: Decimal
    outcome: DecisionOutcome
    inputs: dict[str, Any]


def _finish(action: ActionType, vector: RiskVector, rules: list[HardRuleResult], policy: dict, inputs: dict,
            amount_base: Decimal | None, ablate: dict[str, bool] | None = None, preauthorized: bool = False) -> Assessment:
    # ``ablate`` carries both research ablations (no_evidence, no_uncertainty, no_hard_rules) and the recorded
    # human context (human_approved, escalation_cleared).
    ablate = ablate or {}
    if ablate.get("no_evidence"):
        vector.E = make_factor("E", 0, "ABLATION: evidence factor disabled")
    if ablate.get("no_uncertainty"):
        vector.U = make_factor("U", 0, "ABLATION: uncertainty factor disabled")
    score = risk_score(vector, policy.get("weights"))
    outcome = decide(action, score, rules, policy, amount_base=amount_base,
                     ablate_hard_rules=bool(ablate.get("no_hard_rules")),
                     human_approved=bool(ablate.get("human_approved")),
                     escalation_cleared=bool(ablate.get("escalation_cleared")), preauthorized=preauthorized)
    return Assessment(action, vector, rules, score, outcome, inputs)


# --------------------------------------------------------------------------------------------- payments
@dataclass
class PaymentSnapshot:
    amount: Decimal
    currency: str
    expected_amount: Decimal
    expected_currency: str
    amount_base: Decimal
    due_date: date
    period_start: date
    period_end: date
    contract_start: date
    contract_end: date | None
    contract_status: str
    is_due: bool
    actor_authorized: bool = True
    same_org: bool = True
    actor_is_contractor: bool = False
    version_current: bool = True
    already_paid: bool = False
    active_attempt: bool = False
    unknown_attempt: bool = False
    total_value: Decimal | None = None
    paid_to_date: Decimal = Decimal(0)
    invoice_required: bool = False
    invoice_status: str | None = None
    invoice_amount: Decimal | None = None
    pending_amendment_affects: bool = False
    beneficiary_present: bool = True
    beneficiary_verified: bool = True
    beneficiary_changed: bool = False   # payout account replaced since the last successful payment / contract approval
    # Claim tiering (invoice-required contracts). claim_amount = amount on the contractor's latest invoice; None = no
    # claim yet (legacy behaviour). payout_amount = what would be paid (the claim when tiering applies).
    claim_amount: Decimal | None = None
    payout_amount: Decimal | None = None
    claim_cleared: bool = False         # risk review cleared this exact claim (same amount and payee)
    auto_execute_permitted: bool = False
    auto_execute_enabled: bool = True
    human_approval_granted: bool = False
    extraction_min_confidence: Decimal | None = None
    fields_corrected: int = 0
    document_integrity_ok: bool = True
    injection_flag: bool = False
    first_payment: bool = False
    prior_failed_attempts: int = 0
    is_prorated: bool = False
    trigger: str = "scheduler"
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


def assess_payment(s: PaymentSnapshot, policy: dict, ablate: dict[str, bool] | None = None) -> Assessment:
    r: list[HardRuleResult] = [
        rule("PAY_ACTOR_AUTHORIZED", s.actor_authorized, Decision.BLOCK,
             "Actor or service identity is not authorized to execute payments", terminal=False),
        rule("PAY_ORG_SCOPE", s.same_org, Decision.BLOCK, "Payment is outside the actor's organization scope"),
        rule("PAY_NOT_SELF", not s.actor_is_contractor, Decision.BLOCK,
             "A contractor cannot approve or execute their own payment"),
        rule("PAY_CONTRACT_NOT_EXPIRED",
             s.contract_status not in (ContractStatus.EXPIRED, ContractStatus.TERMINATED, ContractStatus.REJECTED),
             Decision.BLOCK, f"Contract is {s.contract_status}; payments are not permitted", terminal=True),
        rule("PAY_CONTRACT_NOT_SUSPENDED", s.contract_status != ContractStatus.SUSPENDED, Decision.ESCALATE,
             "Contract is suspended; payment held for compliance review"),
        rule("PAY_CONTRACT_ACTIVE",
             s.contract_status in (ContractStatus.ACTIVE, ContractStatus.SUSPENDED, ContractStatus.EXPIRED,
                                   ContractStatus.TERMINATED, ContractStatus.REJECTED),
             Decision.BLOCK, f"Contract is not active (status {s.contract_status})"),
        rule("PAY_WITHIN_CONTRACT_DATES",
             s.contract_start <= s.period_start and (s.contract_end is None or s.period_end <= s.contract_end),
             Decision.BLOCK, "Service period is outside the contract's effective dates", terminal=True),
        rule("PAY_IS_DUE", s.is_due, Decision.BLOCK, "Payment is not yet due; early execution is not permitted"),
        rule("PAY_VERSION_CURRENT", s.version_current, Decision.BLOCK,
             "Scheduled occurrence does not match the currently approved contract version", terminal=True),
        rule("PAY_NOT_DUPLICATE", not (s.already_paid or s.active_attempt), Decision.BLOCK,
             "Duplicate: this occurrence is already paid or has a payout in flight"),
        rule("PAY_NO_UNRECONCILED", not s.unknown_attempt, Decision.ESCALATE,
             "A previous payout attempt has an unknown outcome and must be reconciled first"),
        rule("PAY_AMOUNT_CURRENCY_MATCH", s.amount == s.expected_amount and s.currency == s.expected_currency,
             Decision.BLOCK, f"Amount/currency {s.amount} {s.currency} does not match approved terms "
                             f"{s.expected_amount} {s.expected_currency}", terminal=True),
        rule("PAY_CUMULATIVE_CAP",
             s.total_value is None or s.paid_to_date + (s.payout_amount if s.payout_amount is not None else s.amount) <= s.total_value,
             Decision.ESCALATE,
             "Payment would exceed the contract's total value"),
        rule("PAY_BENEFICIARY_VERIFIED", s.beneficiary_present and s.beneficiary_verified, Decision.VERIFY,
             "A verified payout beneficiary is required"),
        rule("PAY_BENEFICIARY_UNCHANGED", not s.beneficiary_changed or s.claim_cleared, Decision.ESCALATE,
             "Payout account (payee) changed since the contract was approved or last paid; escalated for risk review"),
        rule("PAY_NO_PENDING_AMENDMENT", not s.pending_amendment_affects, Decision.VERIFY,
             "An unresolved amendment affects this payment period; held until it is decided"),
    ]
    tiers = policy.get("invoice_tiers") or {"enabled": True, "approve_tolerance_pct": "10"}
    tiering = s.invoice_required and s.claim_amount is not None and bool(tiers.get("enabled", True))
    preauthorized = False
    if tiering:
        tol = Decimal(str(tiers.get("approve_tolerance_pct", "10")))
        terms = s.expected_amount
        over_pct = ((s.claim_amount - terms) * 100 / terms).quantize(Decimal("0.1")) if terms and terms > 0 else Decimal(0)
        r += [
            rule("PAY_INVOICE_REVIEWED", s.invoice_status != "SUBMITTED", Decision.VERIFY,
                 "Invoice is waiting for finance review (automatic validation is switched off)"),
            rule("PAY_INVOICE_VALID", s.invoice_status != "FLAGGED" or s.claim_cleared, Decision.ESCALATE,
                 "Invoice failed automatic validation; risk review required"),
            rule("PAY_CLAIM_WITHIN_TOLERANCE", s.claim_amount <= terms * (1 + tol / 100) or s.claim_cleared, Decision.ESCALATE,
                 f"Claim {s.claim_amount} {s.currency} is {over_pct}% above the contract amount {terms} (more than {tol}%); "
                 f"risk review required"),
            rule("PAY_CLAIM_WITHIN_TERMS", s.claim_amount <= terms, Decision.APPROVE,
                 f"Claim {s.claim_amount} {s.currency} is {over_pct}% above the contract amount {terms}; approver must "
                 f"approve this exact amount"),
        ]
        preauthorized = s.claim_amount <= terms and s.invoice_status in ("ACCEPTED", "FLAGGED") and \
            (s.invoice_status == "ACCEPTED" or s.claim_cleared)
    elif s.invoice_required:
        if s.invoice_status == "ACCEPTED" and s.invoice_amount is not None and s.invoice_amount != s.amount:
            r.append(rule("PAY_INVOICE_AMOUNT", False, Decision.ESCALATE,
                          f"Accepted invoice amount {s.invoice_amount} differs from scheduled amount {s.amount}"))
        else:
            r.append(rule("PAY_INVOICE_PRESENT", s.invoice_status == "ACCEPTED", Decision.VERIFY,
                          "Contract requires an accepted invoice for this period"
                          + (f" (invoice is {s.invoice_status})" if s.invoice_status else " (none submitted)")))
    r.append(rule("PAY_AUTO_EXECUTION_PERMITTED",
                  s.human_approval_granted or preauthorized or (s.auto_execute_permitted and s.auto_execute_enabled), Decision.APPROVE,
                  "Contract or policy does not permit automatic execution; human approval required"))

    fin, band_note = band(s.amount_base, policy["financial_bands"]["per_payment"])
    sens = 2 + (1 if not s.beneficiary_verified else 0) + (1 if s.injection_flag else 0) + (1 if s.beneficiary_changed else 0)
    e_val, e_why = 0, []
    if s.invoice_required and s.invoice_status != "ACCEPTED":
        e_val += 2; e_why.append("required invoice not accepted (+2)")
    if s.extraction_min_confidence is not None and s.extraction_min_confidence < Decimal(str(policy["min_field_confidence"])):
        e_val += 1; e_why.append(f"lowest critical-field confidence {s.extraction_min_confidence} (+1)")
    if s.fields_corrected:
        e_val += 1; e_why.append(f"{s.fields_corrected} manually corrected field(s) (+1)")
    if not s.document_integrity_ok:
        e_val += 2; e_why.append("source document hash/integrity not verified (+2)")
    if not s.version_current:
        e_val += 2; e_why.append("schedule does not match approved version (+2)")
    u_val, u_why = 0, []
    if s.first_payment:
        u_val += 1; u_why.append("first payment on this contract (+1)")
    if s.prior_failed_attempts:
        u_val += min(2, s.prior_failed_attempts); u_why.append(f"{s.prior_failed_attempts} prior failed attempt(s)")
    if s.unknown_attempt:
        u_val += 2; u_why.append("unknown provider outcome (+2)")
    if s.is_prorated:
        u_val += 1; u_why.append("prorated amount (+1)")
    if s.injection_flag:
        u_val += 2; u_why.append("instruction-like content detected in contract (+2)")
    vector = RiskVector(
        S=make_factor("S", sens, "payout uses masked banking reference (base 2)",
                      "beneficiary not verified (+1)" if not s.beneficiary_verified else "",
                      "suspicious document content (+1)" if s.injection_flag else ""),
        F=make_factor("F", fin, f"amount {s.amount} {s.currency} ≈ {s.amount_base.quantize(Decimal('1'))} base, band {band_note}"),
        I=make_factor("I", 4, "bank payouts are hard to reverse once processed"),
        E=make_factor("E", min(5, e_val), *(e_why or ["approved contract version, document hash and required evidence present"])),
        T=make_factor("T", policy["tool_risk"]["payout.create"], "payout.create moves funds via an external provider"),
        U=make_factor("U", min(5, u_val), *(u_why or ["deterministic amount from approved schedule"])),
        evidence_refs=s.evidence_refs,
    )
    return _finish(ActionType.PAYMENT_EXECUTION, vector, r, policy, _inputs(s), s.amount_base, ablate,
                   preauthorized=preauthorized)


# --------------------------------------------------------------------------------------------- contract approval
@dataclass
class ContractApprovalSnapshot:
    commitment_base: Decimal
    payment_amount_base: Decimal
    contract_status: str
    approver_authorized: bool = True
    same_org: bool = True
    approver_is_contractor: bool = False
    approver_is_validator: bool = False
    allow_validator_as_approver: bool = False
    validation_passed: bool = True
    unresolved_critical_fields: list[str] = field(default_factory=list)
    low_confidence_fields: list[str] = field(default_factory=list)
    conflicting_fields: list[str] = field(default_factory=list)
    corrected_fields: int = 0
    injection_detected: bool = False
    duplicate_active_contract: bool = False
    dates_valid: bool = True
    auto_execute_requested: bool = False
    duration_months: int = 12
    bank_details_in_document: bool = False
    signatures_detected: bool | None = None
    ocr_used: bool = False
    ocr_mean_confidence: Decimal | None = None
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    preview: bool = False  # True when assessing at submission (no approver yet)


def assess_contract_approval(s: ContractApprovalSnapshot, policy: dict,
                             ablate: dict[str, bool] | None = None) -> Assessment:
    r = [
        rule("CA_ORG_SCOPE", s.same_org, Decision.BLOCK, "Contract is outside the actor's organization"),
        rule("CA_DATES_VALID", s.dates_valid, Decision.BLOCK, "Contract dates are missing or inconsistent", terminal=True),
        rule("CA_NO_DUPLICATE", not s.duplicate_active_contract, Decision.BLOCK,
             "A duplicate or conflicting active contract exists for this contractor", terminal=True),
        rule("CA_CRITICAL_FIELDS_RESOLVED", not s.unresolved_critical_fields, Decision.VERIFY,
             f"Unresolved critical fields: {', '.join(s.unresolved_critical_fields)}"),
        rule("CA_NO_CONFLICTS", not s.conflicting_fields, Decision.VERIFY,
             f"Conflicting values must be resolved: {', '.join(s.conflicting_fields)}"),
        rule("CA_NO_INJECTION", not s.injection_detected, Decision.ESCALATE,
             "Document contains instruction-like content aimed at the AI; compliance review required"),
    ]
    if not s.preview:
        r += [
            rule("CA_APPROVER_AUTHORIZED", s.approver_authorized, Decision.BLOCK, "Actor may not approve contracts"),
            rule("CA_NOT_SELF", not s.approver_is_contractor, Decision.BLOCK,
                 "A contractor cannot approve their own contract"),
            rule("CA_VALIDATOR_SOD", s.allow_validator_as_approver or not s.approver_is_validator, Decision.BLOCK,
                 "Separation of duties: the validator cannot also approve this contract"),
            rule("CA_STATUS", s.contract_status == ContractStatus.PENDING_APPROVAL, Decision.BLOCK,
                 f"Contract is {s.contract_status}, not PENDING_APPROVAL"),
            rule("CA_VALIDATION_PASSED", s.validation_passed, Decision.BLOCK, "Validation has not passed"),
        ]
    fin, band_note = band(s.commitment_base, policy["financial_bands"]["commitment"])
    e_val, e_why = 0, []
    if s.unresolved_critical_fields:
        e_val += min(4, 2 * len(s.unresolved_critical_fields)); e_why.append("unresolved critical fields")
    if s.low_confidence_fields:
        e_val += 1; e_why.append(f"low-confidence fields: {', '.join(s.low_confidence_fields[:4])} (+1)")
    if s.signatures_detected is False:
        e_val += 1; e_why.append("no signature block detected (+1)")
    if s.corrected_fields > 2:
        e_val += 1; e_why.append(f"{s.corrected_fields} manual corrections (+1)")
    u_val, u_why = 0, []
    if s.ocr_used and (s.ocr_mean_confidence is None or s.ocr_mean_confidence < Decimal(85)):
        u_val += 1; u_why.append(f"OCR text, mean confidence {s.ocr_mean_confidence} (+1)")
    if s.conflicting_fields:
        u_val += 2; u_why.append("conflicting extracted values (+2)")
    if s.injection_detected:
        u_val += 2; u_why.append("instruction-like content in document (+2)")
    if s.corrected_fields:
        u_val += 1; u_why.append("extraction required correction (+1)")
    vector = RiskVector(
        S=make_factor("S", 3 + (1 if s.bank_details_in_document else 0), "contract terms and personal data (base 3)",
                      "bank instructions present in document (+1)" if s.bank_details_in_document else ""),
        F=make_factor("F", fin, f"total commitment ≈ {s.commitment_base.quantize(Decimal('1'))} base, band {band_note}"),
        I=make_factor("I", 2 + (1 if s.auto_execute_requested else 0) + (1 if s.duration_months > 24 else 0),
                      "approval authorizes a recurring payment obligation (base 2)",
                      "requests automatic execution (+1)" if s.auto_execute_requested else "",
                      "term longer than 24 months (+1)" if s.duration_months > 24 else ""),
        E=make_factor("E", min(5, e_val), *(e_why or ["all critical fields resolved with source evidence"])),
        T=make_factor("T", policy["tool_risk"]["contract.activate"], "activation creates a payment schedule"),
        U=make_factor("U", min(5, u_val), *(u_why or ["native text, consistent values"])),
        evidence_refs=s.evidence_refs,
    )
    return _finish(ActionType.CONTRACT_APPROVAL, vector, r, policy, _inputs(s), s.commitment_base, ablate)


# --------------------------------------------------------------------------------------------- amendments
@dataclass
class AmendmentSnapshot:
    old_amount: Decimal
    new_amount: Decimal
    old_currency: str
    new_currency: str
    commitment_delta_base: Decimal
    new_amount_base: Decimal
    amendment_status: str
    approver_authorized: bool = True
    same_org: bool = True
    approver_is_requester: bool = False
    approver_is_contractor: bool = False
    base_is_current: bool = True
    contract_active: bool = True
    frequency_changed: bool = False
    retroactive_affects_paid: bool = False
    has_document: bool = False
    document_matches: bool | None = None
    injection_detected: bool = False
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    preview: bool = False


def assess_amendment(s: AmendmentSnapshot, policy: dict, ablate: dict[str, bool] | None = None) -> Assessment:
    pct = (abs(s.new_amount - s.old_amount) * 100 / s.old_amount) if s.old_amount else Decimal(100)
    need_doc_pct = Decimal(str(policy["amendment_increase_requires_document_pct"]))
    r = [
        rule("AM_ORG_SCOPE", s.same_org, Decision.BLOCK, "Amendment is outside the actor's organization"),
        rule("AM_CONTRACT_ACTIVE", s.contract_active, Decision.BLOCK, "Only active contracts can be amended", terminal=True),
        rule("AM_BASE_CURRENT", s.base_is_current, Decision.BLOCK,
             "Amendment was raised against a superseded contract version", terminal=True),
        rule("AM_NOT_RETROACTIVE", not s.retroactive_affects_paid, Decision.BLOCK,
             "Effective date would alter already paid or in-flight payments", terminal=True),
        rule("AM_CURRENCY_UNCHANGED", s.old_currency == s.new_currency, Decision.ESCALATE,
             f"Currency change {s.old_currency}→{s.new_currency} requires compliance review"),
        rule("AM_SUPPORTING_DOCUMENT", s.has_document or s.new_amount <= s.old_amount or pct <= need_doc_pct,
             Decision.VERIFY, f"Increase of {pct.quantize(Decimal('0.1'))}% requires a signed supporting document"),
        rule("AM_DOCUMENT_MATCHES", s.document_matches is not False, Decision.ESCALATE,
             "Amendment document does not support the proposed values"),
        rule("AM_NO_INJECTION", not s.injection_detected, Decision.ESCALATE,
             "Amendment document contains instruction-like content aimed at the AI"),
    ]
    if not s.preview:
        r += [
            rule("AM_APPROVER_AUTHORIZED", s.approver_authorized, Decision.BLOCK, "Actor may not approve amendments"),
            rule("AM_NOT_SELF", not (s.approver_is_requester or s.approver_is_contractor), Decision.BLOCK,
                 "The requester/contractor cannot approve their own amendment"),
            rule("AM_STATUS", s.amendment_status == "PENDING_APPROVAL", Decision.BLOCK,
                 f"Amendment is {s.amendment_status}, not PENDING_APPROVAL"),
        ]
    fin_delta, note_delta = band(abs(s.commitment_delta_base), policy["financial_bands"]["commitment"])
    fin_pay, note_pay = band(s.new_amount_base, policy["financial_bands"]["per_payment"])
    e_val, e_why = 0, []
    if not s.has_document:
        e_val += 2; e_why.append("no supporting amendment document (+2)")
    if s.document_matches is False:
        e_val += 2; e_why.append("document does not match proposed values (+2)")
    if pct > 25:
        e_val += 1; e_why.append(f"large change {pct.quantize(Decimal('0.1'))}% (+1)")
    u_val, u_why = 0, []
    if s.frequency_changed:
        u_val += 1; u_why.append("payment frequency changes (+1)")
    if s.injection_detected:
        u_val += 2; u_why.append("instruction-like content (+2)")
    if pct > 50:
        u_val += 1; u_why.append("change exceeds 50% (+1)")
    vector = RiskVector(
        S=make_factor("S", 3, "amends contractual payment terms"),
        F=make_factor("F", max(fin_delta, fin_pay), f"commitment change band {note_delta}; new per-payment band {note_pay}"),
        I=make_factor("I", 3, "changes future payment obligations"),
        E=make_factor("E", min(5, e_val), *(e_why or ["supporting document matches proposed terms"])),
        T=make_factor("T", policy["tool_risk"]["contract.amend"], "creates a new contract version and regenerates schedule"),
        U=make_factor("U", min(5, u_val), *(u_why or ["amount-only change with deterministic comparison"])),
        evidence_refs=s.evidence_refs,
    )
    return _finish(ActionType.AMENDMENT_APPROVAL, vector, r, policy, _inputs(s), abs(s.commitment_delta_base), ablate)


# --------------------------------------------------------------------------------------------- agent
@dataclass
class AgentSnapshot:
    tool: str
    authorized: bool
    data_sensitivity: int
    evidence_coverage: Decimal  # 0..1
    conflicting_evidence: bool = False
    injection_detected: bool = False
    amount_base: Decimal = Decimal(0)
    writes: bool = False
    # Factual answers must be grounded: missing mandatory evidence is a deterministic VERIFY override.
    evidence_required: bool = False
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


MIN_EVIDENCE_COVERAGE = Decimal("0.5")


def assess_agent(s: AgentSnapshot, policy: dict, ablate: dict[str, bool] | None = None) -> Assessment:
    action = ActionType.AGENT_TOOL if s.writes else ActionType.AGENT_QUERY
    r = [
        rule("AG_TOOL_ALLOWLISTED", s.tool in policy["tool_risk"], Decision.BLOCK, f"Tool {s.tool} is not allowlisted"),
        rule("AG_AUTHORIZED", s.authorized, Decision.BLOCK, "Delegating user lacks permission for this tool/resource"),
        rule("AG_NO_INJECTION", not s.injection_detected, Decision.ESCALATE,
             "Request or retrieved content attempts to instruct the agent; ignored and escalated"),
        rule("AG_EVIDENCE_PRESENT", not s.evidence_required or s.evidence_coverage >= MIN_EVIDENCE_COVERAGE,
             Decision.VERIFY, "Supporting evidence is missing; retrieve or request the source document before answering"),
    ]
    fin, note = band(s.amount_base, policy["financial_bands"]["per_payment"]) if s.amount_base else (0, "read-only")
    cov = max(Decimal(0), min(Decimal(1), s.evidence_coverage))
    e = (1 - cov) * 5 + (1 if s.conflicting_evidence else 0)
    vector = RiskVector(
        S=make_factor("S", s.data_sensitivity, "classification of data accessed"),
        F=make_factor("F", fin, note),
        I=make_factor("I", 2 if s.writes else 0, "creates a governed workflow request" if s.writes else "read-only"),
        E=make_factor("E", e, f"evidence coverage {cov}", "conflicting evidence (+1)" if s.conflicting_evidence else ""),
        T=make_factor("T", policy["tool_risk"].get(s.tool, 5), f"tool {s.tool}"),
        U=make_factor("U", (1 - cov) * 3 + (2 if s.injection_detected else 0), "uncertainty from evidence gaps"),
        evidence_refs=s.evidence_refs,
    )
    return _finish(action, vector, r, policy, _inputs(s), s.amount_base or None, ablate)


def _inputs(snapshot: Any) -> dict[str, Any]:
    from dataclasses import asdict

    d = asdict(snapshot)
    d.pop("evidence_refs", None)
    return d
