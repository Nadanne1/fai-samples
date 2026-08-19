"""State definitions for LangGraph StateGraph agents."""

from typing import TypedDict


class ScreeningState(TypedDict):
    """State for the Screening Agent StateGraph."""

    trial_id: str
    patient_id: str
    session_id: str
    questionnaire: dict  # FHIR Questionnaire
    patient_fhir_data: dict  # Patient FHIR Bundle
    current_item_index: int
    responses: list  # collected answers
    discrepancies: list  # flagged discrepancies
    eligibility_criteria_results: list  # per-criterion pass/fail
    eligibility_determination: str  # eligible|ineligible|borderline
    messages: list  # conversation history
    deep_dive_active: str | None  # active deep-dive module
    missing_items: list  # required items without responses
    declined_items: list  # items patient declined


class MonitoringState(TypedDict):
    """State for the Monitoring Agent StateGraph."""

    trial_id: str
    patient_id: str
    session_id: str
    visit_number: int
    monitoring_questionnaire: dict
    prior_visits: list
    new_symptoms: list
    new_medications: list
    adverse_events: list
    messages: list
