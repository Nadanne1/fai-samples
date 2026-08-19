"""Type aliases for FHIR resource structures used throughout the system."""

from typing import TypedDict


class EligibilityRule(TypedDict):
    """A single parsed eligibility criterion from a trial protocol."""

    criterion_id: str
    description: str
    criterion_type: str  # inclusion | exclusion
    data_type: str  # boolean, integer, decimal, string, date, coding
    fhir_path: str  # e.g. Patient.birthDate, Condition.code
    operator: str  # equals, age_between, exists, contains, etc.
    value: object  # expected value or range
    terminology_codes: list[dict]  # [{system, code, display}]


class QuestionnaireItem(TypedDict, total=False):
    """Simplified representation of a FHIR Questionnaire item."""

    linkId: str
    text: str
    type: str  # boolean, decimal, integer, date, string, choice, group
    required: bool
    code: list[dict]
    enableWhen: list[dict]
    item: list["QuestionnaireItem"]


# Full FHIR resource type aliases (dict-based for flexibility with FHIR R4)
FHIRQuestionnaire = dict
FHIRQuestionnaireResponse = dict
FHIRPatient = dict
FHIRCondition = dict
FHIRObservation = dict
FHIRMedicationRequest = dict
FHIRAllergyIntolerance = dict
FHIRProcedure = dict
FHIRBundle = dict
