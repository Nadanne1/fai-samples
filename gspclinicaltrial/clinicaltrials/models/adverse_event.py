"""Adverse event data model."""

from typing import Literal, TypedDict


class SuspectMedication(TypedDict):
    """Medication involved in an adverse event."""

    name: str
    who_drug_code: str
    rxnorm_code: str


class AdverseEvent(TypedDict):
    """Adverse event reported during monitoring visits."""

    ae_id: str
    patient_id: str
    trial_id: str
    session_id: str
    description: str
    meddra_pt: str  # MedDRA Preferred Term
    meddra_soc: str  # MedDRA System Organ Class
    onset_date: str  # ISO 8601
    severity: Literal["mild", "moderate", "severe"]
    seriousness: list[str]  # hospitalization, life-threatening, death, etc.
    causality: str  # related, possibly_related, unlikely, unrelated
    outcome: str  # recovered, recovering, not_recovered, fatal, unknown
    suspect_medications: list[SuspectMedication]
    concomitant_medications: list[SuspectMedication]
    reporter: str  # patient | investigator | sponsor
    report_date: str  # ISO 8601
    regulatory_deadline: str  # ISO 8601
