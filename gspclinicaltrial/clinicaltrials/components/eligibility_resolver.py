"""Eligibility Resolver — per-criterion evaluation and final determination.

Evaluates patient-reported responses and FHIR records against trial protocol
eligibility criteria. Detects discrepancies between self-reported data and
medical records, classifies severity, and produces a final eligibility
determination (eligible / ineligible / borderline) with a full per-criterion
breakdown and FHIR resource references.

Also handles FHIR resource deduplication when merging external EHR data with
local HealthLake data (Requirement 11.3).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operator evaluation helpers
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = {"integer", "decimal"}


def _parse_numeric(value: Any) -> float | None:
    """Attempt to coerce *value* to a float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_str(value: Any) -> str:
    """Lower-case, stripped string representation."""
    if value is None:
        return ""
    return str(value).strip().lower()


def _evaluate_operator(
    operator: str,
    patient_value: Any,
    expected_value: Any,
    data_type: str,
) -> bool | None:
    """Evaluate an eligibility-rule operator.

    Returns True (pass), False (fail), or None (indeterminate).
    """
    if operator == "equals":
        if data_type in _NUMERIC_TYPES:
            pv = _parse_numeric(patient_value)
            ev = _parse_numeric(expected_value)
            if pv is None or ev is None:
                return None
            return pv == ev
        return _normalise_str(patient_value) == _normalise_str(expected_value)

    if operator == "not_equals":
        result = _evaluate_operator("equals", patient_value, expected_value, data_type)
        return None if result is None else not result

    if operator == "age_between":
        # expected_value is [min, max]
        pv = _parse_numeric(patient_value)
        if pv is None or not isinstance(expected_value, (list, tuple)) or len(expected_value) < 2:
            return None
        lo = _parse_numeric(expected_value[0])
        hi = _parse_numeric(expected_value[1])
        if lo is None or hi is None:
            return None
        return lo <= pv <= hi

    if operator in ("greater_than", "gt"):
        pv = _parse_numeric(patient_value)
        ev = _parse_numeric(expected_value)
        if pv is None or ev is None:
            return None
        return pv > ev

    if operator in ("less_than", "lt"):
        pv = _parse_numeric(patient_value)
        ev = _parse_numeric(expected_value)
        if pv is None or ev is None:
            return None
        return pv < ev

    if operator in ("greater_than_or_equal", "gte"):
        pv = _parse_numeric(patient_value)
        ev = _parse_numeric(expected_value)
        if pv is None or ev is None:
            return None
        return pv >= ev

    if operator in ("less_than_or_equal", "lte"):
        pv = _parse_numeric(patient_value)
        ev = _parse_numeric(expected_value)
        if pv is None or ev is None:
            return None
        return pv <= ev

    if operator == "exists":
        # patient_value should be truthy when the data point exists
        if isinstance(patient_value, bool):
            expected_bool = expected_value if isinstance(expected_value, bool) else True
            return patient_value == expected_bool
        return patient_value is not None and patient_value != ""

    if operator == "contains":
        if isinstance(patient_value, list):
            return any(_normalise_str(expected_value) in _normalise_str(v) for v in patient_value)
        return _normalise_str(expected_value) in _normalise_str(patient_value)

    if operator == "in":
        # expected_value is a list of acceptable values
        if not isinstance(expected_value, list):
            return None
        normalised_expected = [_normalise_str(v) for v in expected_value]
        if isinstance(patient_value, list):
            return any(_normalise_str(v) in normalised_expected for v in patient_value)
        return _normalise_str(patient_value) in normalised_expected

    if operator == "between":
        pv = _parse_numeric(patient_value)
        if pv is None or not isinstance(expected_value, (list, tuple)) or len(expected_value) < 2:
            return None
        lo = _parse_numeric(expected_value[0])
        hi = _parse_numeric(expected_value[1])
        if lo is None or hi is None:
            return None
        return lo <= pv <= hi

    # Unknown operator → indeterminate
    logger.warning("Unknown operator '%s' — returning indeterminate", operator)
    return None


# ---------------------------------------------------------------------------
# FHIR resource helpers
# ---------------------------------------------------------------------------

def _extract_fhir_value(fhir_data: dict, fhir_path: str, resource_type: str) -> tuple[Any, list[str]]:
    """Extract a value from patient FHIR data for a given fhir_path.

    Returns (value, list_of_fhir_resource_references).
    """
    refs: list[str] = []
    resources = fhir_data.get(resource_type, [])

    if not resources:
        return None, refs

    # Simple path resolution for common patterns
    if fhir_path == "Patient.birthDate":
        for r in resources:
            bd = r.get("birthDate")
            if bd:
                refs.append(f"Patient/{r.get('id', '')}")
                return bd, refs

    if resource_type == "Condition":
        all_codes = []
        for r in resources:
            refs.append(f"Condition/{r.get('id', '')}")
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                code = c.get("code")
                if code:
                    all_codes.append(code)
            text = r.get("code", {}).get("text")
            if text:
                all_codes.append(text)
        return all_codes if all_codes else None, refs

    if resource_type == "Observation":
        for r in resources:
            vq = r.get("valueQuantity", {})
            if vq:
                refs.append(f"Observation/{r.get('id', '')}")
                return vq.get("value"), refs
            vc = r.get("valueCodeableConcept", {})
            if vc:
                refs.append(f"Observation/{r.get('id', '')}")
                codings = vc.get("coding", [])
                if codings:
                    return codings[0].get("code", ""), refs

    if resource_type == "MedicationRequest":
        for r in resources:
            med = r.get("medicationCodeableConcept", {})
            codings = med.get("coding", [])
            refs.append(f"MedicationRequest/{r.get('id', '')}")
            if codings:
                return codings[0].get("code", ""), refs

    if resource_type == "AllergyIntolerance":
        for r in resources:
            codes = r.get("code", {}).get("coding", [])
            refs.append(f"AllergyIntolerance/{r.get('id', '')}")
            for c in codes:
                return c.get("code", ""), refs

    if resource_type == "Procedure":
        for r in resources:
            codes = r.get("code", {}).get("coding", [])
            refs.append(f"Procedure/{r.get('id', '')}")
            for c in codes:
                return c.get("code", ""), refs

    return None, refs


def _resource_type_for_path(fhir_path: str) -> str:
    """Infer the FHIR resource type from a fhir_path string."""
    prefix = fhir_path.split(".")[0] if fhir_path else ""
    return prefix if prefix else "Patient"


# ---------------------------------------------------------------------------
# Discrepancy detection
# ---------------------------------------------------------------------------

DiscrepancySeverity = Literal["informational", "warning", "critical"]


def _classify_discrepancy(
    criterion_type: str,
    data_type: str,
    patient_value: Any,
    fhir_value: Any,
) -> DiscrepancySeverity:
    """Classify the severity of a discrepancy between patient-reported and FHIR values.

    - critical: boolean disagreement on inclusion/exclusion criteria, or large
      numeric deviation (>50 %).
    - warning: moderate numeric deviation (10-50 %) or string mismatch on
      required criteria.
    - informational: minor differences (< 10 % numeric, or cosmetic string
      differences).
    """
    # Boolean disagreement on eligibility criteria is always critical
    if data_type == "boolean":
        return "critical"

    # Numeric deviation
    pv = _parse_numeric(patient_value)
    fv = _parse_numeric(fhir_value)
    if pv is not None and fv is not None and fv != 0:
        deviation = abs(pv - fv) / abs(fv)
        if deviation > 0.5:
            return "critical"
        if deviation > 0.1:
            return "warning"
        return "informational"

    # String-based: any mismatch on exclusion criteria is critical
    if criterion_type == "exclusion":
        return "critical"

    return "warning"


def _detect_discrepancy(
    patient_value: Any,
    fhir_value: Any,
    criterion: dict,
) -> dict | None:
    """Compare patient-reported value with FHIR record value.

    Returns a discrepancy dict or None if values agree.
    """
    if fhir_value is None:
        # No FHIR data to compare — no discrepancy detectable
        return None

    data_type = criterion.get("data_type", "string")

    # Normalise for comparison
    if data_type in _NUMERIC_TYPES:
        pv = _parse_numeric(patient_value)
        fv = _parse_numeric(fhir_value)
        if pv is not None and fv is not None and pv == fv:
            return None
    elif data_type == "boolean":
        pb = str(patient_value).strip().lower() in ("true", "1", "yes")
        # If fhir_value is a list (e.g. condition codes), treat as truthy when non-empty
        if isinstance(fhir_value, list):
            fb = len(fhir_value) > 0
        else:
            fb = str(fhir_value).strip().lower() in ("true", "1", "yes")
        if pb == fb:
            return None
    else:
        if isinstance(fhir_value, list):
            # No discrepancy if the patient-reported value matches any element in the list
            if _normalise_str(patient_value) in [_normalise_str(v) for v in fhir_value]:
                return None
        elif _normalise_str(patient_value) == _normalise_str(fhir_value):
            return None

    severity = _classify_discrepancy(
        criterion.get("criterion_type", "inclusion"),
        data_type,
        patient_value,
        fhir_value,
    )

    return {
        "criterion_id": criterion.get("criterion_id", ""),
        "severity": severity,
        "patient_reported": patient_value,
        "fhir_record": fhir_value,
        "description": (
            f"Discrepancy on {criterion.get('criterion_id', '?')}: "
            f"patient reported '{patient_value}' vs FHIR record '{fhir_value}'"
        ),
    }


# ---------------------------------------------------------------------------
# FHIR resource deduplication (Requirement 11.3)
# ---------------------------------------------------------------------------

def deduplicate_fhir_resources(
    local_data: dict[str, list[dict]],
    external_data: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Merge external EHR FHIR resources with local HealthLake data.

    Deduplicates by (resource_type, clinical_code) so that the same clinical
    concept is not counted twice when data comes from both sources.

    Args:
        local_data: Resource-type → list of FHIR resources from HealthLake.
        external_data: Resource-type → list of FHIR resources from external EHR.

    Returns:
        Merged dict with deduplicated resource lists.
    """
    merged: dict[str, list[dict]] = {}

    all_types = set(local_data.keys()) | set(external_data.keys())
    # Skip non-resource keys injected by the EHR tool
    skip_keys = {"ehr_query_failed", "failure_reason"}

    for rtype in all_types:
        if rtype in skip_keys:
            continue

        local_resources = local_data.get(rtype, [])
        external_resources = external_data.get(rtype, [])

        if not external_resources:
            merged[rtype] = list(local_resources)
            continue

        # Build a set of "identity keys" from local resources
        seen_keys: set[str] = set()
        deduped: list[dict] = []

        for r in local_resources:
            key = _resource_identity_key(rtype, r)
            seen_keys.add(key)
            deduped.append(r)

        for r in external_resources:
            key = _resource_identity_key(rtype, r)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(r)

        merged[rtype] = deduped

    return merged


def _resource_identity_key(resource_type: str, resource: dict) -> str:
    """Produce a deduplication key for a FHIR resource.

    Uses the first clinical code (system|code) when available, falling back
    to the resource id.
    """
    # Try to extract a clinical code
    code_field = resource.get("code", {})
    codings = code_field.get("coding", []) if isinstance(code_field, dict) else []

    if not codings and resource_type == "MedicationRequest":
        med = resource.get("medicationCodeableConcept", {})
        codings = med.get("coding", []) if isinstance(med, dict) else []

    if not codings and resource_type == "Observation":
        code_field = resource.get("code", {})
        codings = code_field.get("coding", []) if isinstance(code_field, dict) else []

    if codings:
        first = codings[0]
        return f"{resource_type}|{first.get('system', '')}|{first.get('code', '')}"

    # Fallback to resource id
    rid = resource.get("id", "")
    return f"{resource_type}|id|{rid}"


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------


def _parse_rule_from_item(questionnaire_item: dict) -> dict | None:
    """Extract the eligibility rule metadata from a Questionnaire item extension."""
    for ext in questionnaire_item.get("extension", []):
        if ext.get("url") == "http://example.org/fhir/StructureDefinition/eligibility-rule":
            try:
                return json.loads(ext["valueString"])
            except (json.JSONDecodeError, KeyError):
                return None
    return None


@tool
def evaluate_criterion(
    criterion: dict,
    patient_response: Any,
    fhir_data: dict,
) -> dict:
    """Evaluate a single eligibility criterion against patient response and FHIR records.

    Compares the patient-reported answer with the criterion's expected value
    using the operator defined in the eligibility rule. Also checks for
    discrepancies between the patient response and corresponding FHIR records.

    Args:
        criterion: An eligibility rule dict (or FHIR Questionnaire item with
            eligibility-rule extension) containing at minimum:
            - criterion_id (str)
            - description (str)
            - criterion_type (str): inclusion | exclusion
            - data_type (str): boolean, integer, decimal, string, date, coding
            - fhir_path (str)
            - operator (str)
            - value: expected value or range
        patient_response: The patient's answer for this criterion.
        fhir_data: Dict of resource_type → list[FHIR resource] from
            healthlake_query_patient (and optionally merged EHR data).

    Returns:
        Dict with keys:
            - criterion_id (str)
            - result: "pass" | "fail" | "indeterminate"
            - discrepancy: dict | None  (severity, patient_reported, fhir_record)
            - fhir_refs: list[str]  FHIR resource references used
    """
    # If the criterion is a Questionnaire item, extract the embedded rule
    rule = _parse_rule_from_item(criterion)
    if rule:
        cid = criterion.get("linkId", "")
        description = criterion.get("text", "")
        criterion_type = rule.get("type", "inclusion")
        data_type = criterion.get("type", "boolean")
        fhir_path = rule.get("fhir_path", "")
        operator = rule.get("operator", "equals")
        expected_value = rule.get("value")
    else:
        # Direct eligibility-rule dict
        cid = criterion.get("criterion_id", "")
        description = criterion.get("description", "")
        criterion_type = criterion.get("criterion_type", "inclusion")
        data_type = criterion.get("data_type", "boolean")
        fhir_path = criterion.get("fhir_path", "")
        operator = criterion.get("operator", "equals")
        expected_value = criterion.get("value")

    # Determine which FHIR resource type to look up
    resource_type = _resource_type_for_path(fhir_path)

    # Extract the corresponding value from FHIR records
    fhir_value, fhir_refs = _extract_fhir_value(fhir_data, fhir_path, resource_type)

    # Evaluate the criterion operator against the patient response
    op_result = _evaluate_operator(operator, patient_response, expected_value, data_type)

    if op_result is True:
        result = "pass" if criterion_type == "inclusion" else "fail"
    elif op_result is False:
        result = "fail" if criterion_type == "inclusion" else "pass"
    else:
        result = "indeterminate"

    # Detect discrepancy between patient-reported and FHIR record
    discrepancy = _detect_discrepancy(
        patient_response,
        fhir_value,
        {
            "criterion_id": cid,
            "criterion_type": criterion_type,
            "data_type": data_type,
        },
    )

    return {
        "criterion_id": cid,
        "result": result,
        "discrepancy": discrepancy,
        "fhir_refs": fhir_refs,
    }


@tool
def resolve_eligibility(criteria_results: list[dict]) -> dict:
    """Produce a final eligibility determination from per-criterion results.

    Aggregates individual criterion evaluations into an overall determination
    of eligible, ineligible, or borderline with a full per-criterion breakdown
    and FHIR resource references.

    Rules:
    - If any criterion result is "fail" → ineligible.
    - If all criterion results are "pass" → eligible.
    - Otherwise (any "indeterminate" with no "fail") → borderline.

    Args:
        criteria_results: List of dicts from evaluate_criterion, each with
            criterion_id, result, discrepancy, and fhir_refs.

    Returns:
        Dict with keys:
            - determination: "eligible" | "ineligible" | "borderline"
            - breakdown: list of per-criterion results
            - fhir_refs: aggregated list of all FHIR resource references
            - has_critical_discrepancies: bool
    """
    if not criteria_results:
        return {
            "determination": "borderline",
            "breakdown": [],
            "fhir_refs": [],
            "has_critical_discrepancies": False,
        }

    all_fhir_refs: list[str] = []
    has_fail = False
    has_indeterminate = False
    has_critical = False

    for cr in criteria_results:
        all_fhir_refs.extend(cr.get("fhir_refs", []))

        r = cr.get("result", "indeterminate")
        if r == "fail":
            has_fail = True
        elif r == "indeterminate":
            has_indeterminate = True

        disc = cr.get("discrepancy")
        if disc and disc.get("severity") == "critical":
            has_critical = True

    if has_fail:
        determination = "ineligible"
    elif has_indeterminate or has_critical:
        determination = "borderline"
    else:
        determination = "eligible"

    # Deduplicate FHIR refs while preserving order
    seen: set[str] = set()
    unique_refs: list[str] = []
    for ref in all_fhir_refs:
        if ref not in seen:
            seen.add(ref)
            unique_refs.append(ref)

    return {
        "determination": determination,
        "breakdown": criteria_results,
        "fhir_refs": unique_refs,
        "has_critical_discrepancies": has_critical,
    }
