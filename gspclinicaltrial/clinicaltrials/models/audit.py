"""Audit trail data models for 21 CFR Part 11 compliance."""

from typing import Literal, TypedDict


AuditEventType = Literal[
    "question_asked",
    "response_received",
    "validation_performed",
    "decision_made",
    "data_access",
    "escalation",
    "phi_violation",
    "electronic_signature",
    "audit_write_failure",
]


class ElectronicSignature(TypedDict, total=False):
    """21 CFR Part 11 electronic signature fields."""

    signer_id: str
    signature_timestamp: str  # ISO 8601
    meaning: str


class AuditRecord(TypedDict, total=False):
    """Audit record stored in DynamoDB TrialAuditLog table."""

    audit_id: str
    session_id: str  # partition key
    timestamp: str  # sort key, ISO 8601
    patient_id: str
    trial_id: str
    event_type: AuditEventType
    agent_id: str  # screening-agent | monitoring-agent | protocol-engine
    user_identity: str
    event_data: dict  # encrypted event-specific details
    fhir_resource_refs: list[str]
    phi_encrypted: bytes  # KMS-encrypted PHI data
    signature: ElectronicSignature
    ttl: int  # epoch seconds, minimum 15 years from creation
