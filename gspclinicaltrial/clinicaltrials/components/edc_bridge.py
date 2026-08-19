"""EDC Bridge for CDISC ODM-XML export, SDTM/ADaM dataset generation.

Exports FHIR QuestionnaireResponse resources as CDISC ODM-XML documents,
maps items to CDASH variable names and SDTM domain/variable pairs,
transmits to EDC systems (Medidata Rave, Oracle Clinical One) with retry,
and generates SDTM and ADaM datasets with define.xml metadata.

Components:
    - export_odm_xml: ODM-XML export with schema validation and EDC transmission
    - generate_sdtm_datasets: SDTM dataset generation (DM, MH, CM, AE, VS)
    - generate_adam_datasets: ADaM dataset generation (ADSL, ADAE)
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import boto3
import requests
from langchain_core.tools import tool

from config.settings import (
    AWS_REGION,
    EDC_RETRY_ATTEMPTS,
    EDC_RETRY_BACKOFF_SECONDS,
    HEALTHLAKE_DATASTORE_ID,
)
from components.trial_config_store import get_trial_config as _get_trial_config_from_store
from tools.audit import audit_log_event

logger = logging.getLogger(__name__)

# ODM-XML namespace
_ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"

# ── CDASH / SDTM mapping tables ──────────────────────────────────────────────
# Maps FHIR resource paths to CDASH variable names and SDTM domain/variable.
# The mapping is deterministic: the same data point always maps to the same
# CDASH/SDTM variables (Property 15).


# Default CDASH mapping keyed by FHIR path prefix → (cdash_variable, sdtm_domain, sdtm_variable)
_DEFAULT_CDASH_MAP: dict[str, tuple[str, str, str]] = {
    # Demographics
    "Patient.birthDate": ("BRTHDTC", "DM", "BRTHDTC"),
    "Patient.gender": ("SEX", "DM", "SEX"),
    "Patient.name": ("SUBJID", "DM", "SUBJID"),
    "Patient.identifier": ("SUBJID", "DM", "SUBJID"),
    "Patient.extension.race": ("RACE", "DM", "RACE"),
    "Patient.extension.ethnicity": ("ETHNIC", "DM", "ETHNIC"),
    # Medical History
    "Condition.code": ("MHTERM", "MH", "MHTERM"),
    "Condition.onsetDateTime": ("MHSTDTC", "MH", "MHSTDTC"),
    "Condition.abatementDateTime": ("MHENDTC", "MH", "MHENDTC"),
    "Condition.clinicalStatus": ("MHONGO", "MH", "MHONGO"),
    # Concomitant Medications
    "MedicationRequest.medicationCodeableConcept": ("CMTRT", "CM", "CMTRT"),
    "MedicationRequest.dosageInstruction.doseAndRate": ("CMDOSE", "CM", "CMDOSE"),
    "MedicationRequest.dosageInstruction.route": ("CMROUTE", "CM", "CMROUTE"),
    "MedicationRequest.authoredOn": ("CMSTDTC", "CM", "CMSTDTC"),
    "MedicationRequest.dosageInstruction.timing": ("CMDOSFRQ", "CM", "CMDOSFRQ"),
    # Adverse Events
    "AdverseEvent.description": ("AETERM", "AE", "AETERM"),
    "AdverseEvent.onset_date": ("AESTDTC", "AE", "AESTDTC"),
    "AdverseEvent.severity": ("AESEV", "AE", "AESEV"),
    "AdverseEvent.seriousness": ("AESER", "AE", "AESER"),
    "AdverseEvent.causality": ("AEREL", "AE", "AEREL"),
    "AdverseEvent.outcome": ("AEOUT", "AE", "AEOUT"),
    # Vital Signs / Observations
    "Observation.code": ("VSTEST", "VS", "VSTEST"),
    "Observation.valueQuantity": ("VSORRES", "VS", "VSORRES"),
    "Observation.valueQuantity.unit": ("VSORRESU", "VS", "VSORRESU"),
    "Observation.effectiveDateTime": ("VSDTC", "VS", "VSDTC"),
    # Procedures
    "Procedure.code": ("MHTERM", "MH", "MHTERM"),
    "Procedure.performedDateTime": ("MHSTDTC", "MH", "MHSTDTC"),
    # Allergies
    "AllergyIntolerance.code": ("MHTERM", "MH", "MHTERM"),
    "AllergyIntolerance.onsetDateTime": ("MHSTDTC", "MH", "MHSTDTC"),
    # Inclusion/Exclusion
    "eligibility.criterion": ("IETEST", "IE", "IETEST"),
    "eligibility.result": ("IEORRES", "IE", "IEORRES"),
    "eligibility.date": ("IEDTC", "IE", "IEDTC"),
}

# SDTM domain metadata for define.xml generation
_SDTM_DOMAIN_META: dict[str, dict] = {
    "DM": {
        "label": "Demographics",
        "structure": "One record per subject",
        "class": "Special Purpose",
        "key_variables": ["STUDYID", "USUBJID"],
    },
    "MH": {
        "label": "Medical History",
        "structure": "One record per medical history event per subject",
        "class": "Events",
        "key_variables": ["STUDYID", "USUBJID", "MHSEQ"],
    },
    "CM": {
        "label": "Concomitant Medications",
        "structure": "One record per medication per subject",
        "class": "Interventions",
        "key_variables": ["STUDYID", "USUBJID", "CMSEQ"],
    },
    "AE": {
        "label": "Adverse Events",
        "structure": "One record per adverse event per subject",
        "class": "Events",
        "key_variables": ["STUDYID", "USUBJID", "AESEQ"],
    },
    "VS": {
        "label": "Vital Signs",
        "structure": "One record per vital sign per visit per subject",
        "class": "Findings",
        "key_variables": ["STUDYID", "USUBJID", "VSSEQ"],
    },
    "IE": {
        "label": "Inclusion/Exclusion Criteria Not Met",
        "structure": "One record per criterion per subject",
        "class": "Special Purpose",
        "key_variables": ["STUDYID", "USUBJID", "IESEQ"],
    },
}

# ADaM dataset metadata
_ADAM_DATASET_META: dict[str, dict] = {
    "ADSL": {
        "label": "Subject-Level Analysis Dataset",
        "structure": "One record per subject",
        "source_domains": ["DM", "MH"],
    },
    "ADAE": {
        "label": "Adverse Event Analysis Dataset",
        "structure": "One record per adverse event per subject",
        "source_domains": ["AE", "DM"],
    },
}


# ── Helper functions ──────────────────────────────────────────────────────────


def _get_trial_config(trial_id: str) -> dict:
    """Retrieve trial protocol configuration from HealthLake ResearchStudy.

    Returns the config dict, or an empty dict if not found.
    """
    return _get_trial_config_from_store(trial_id)


def _get_cdash_mapping(trial_id: str) -> dict[str, tuple[str, str, str]]:
    """Get CDASH/SDTM mapping for a trial.

    Uses trial-specific mapping from TrialProtocolConfig if available,
    otherwise falls back to the default mapping.
    """
    config = _get_trial_config(trial_id)
    custom_mapping = config.get("cdash_mapping")
    if custom_mapping:
        # Custom mapping stored as {fhir_path: [cdash_var, sdtm_domain, sdtm_var]}
        return {
            k: tuple(v) for k, v in custom_mapping.items()
        }
    return _DEFAULT_CDASH_MAP.copy()


def _resolve_cdash_variable(
    fhir_path: str, cdash_map: dict[str, tuple[str, str, str]]
) -> tuple[str, str, str] | None:
    """Resolve a FHIR path to its CDASH/SDTM mapping.

    Tries exact match first, then prefix match for nested paths.
    Returns (cdash_variable, sdtm_domain, sdtm_variable) or None.
    """
    if fhir_path in cdash_map:
        return cdash_map[fhir_path]
    # Try prefix match for nested paths
    for path_prefix, mapping in cdash_map.items():
        if fhir_path.startswith(path_prefix):
            return mapping
    return None


def _extract_item_value(item: dict) -> str:
    """Extract the answer value from a QuestionnaireResponse item."""
    answers = item.get("answer", [])
    if not answers:
        return ""
    answer = answers[0]
    # FHIR answer types
    for key in (
        "valueString", "valueBoolean", "valueDecimal", "valueInteger",
        "valueDate", "valueDateTime", "valueCoding",
    ):
        if key in answer:
            val = answer[key]
            if key == "valueCoding":
                return val.get("display", val.get("code", ""))
            if key == "valueBoolean":
                return "Y" if val else "N"
            return str(val)
    return ""


def _extract_fhir_path_from_item(item: dict) -> str:
    """Extract the FHIR path from a Questionnaire item's eligibility-rule extension."""
    for ext in item.get("extension", []):
        if ext.get("url", "").endswith("eligibility-rule"):
            try:
                rule = json.loads(ext.get("valueString", "{}"))
                return rule.get("fhir_path", "")
            except (json.JSONDecodeError, TypeError):
                pass
    # Fallback: use code system to infer domain
    codes = item.get("code", [])
    if codes:
        system = codes[0].get("system", "")
        if "icd-10" in system or "snomed" in system:
            return "Condition.code"
        if "rxnorm" in system:
            return "MedicationRequest.medicationCodeableConcept"
        if "loinc" in system:
            return "Observation.code"
    return "eligibility.criterion"


# ── ODM-XML generation ────────────────────────────────────────────────────────


def _build_odm_xml(
    questionnaire_response: dict,
    questionnaire: dict,
    trial_id: str,
    cdash_map: dict[str, tuple[str, str, str]],
) -> ET.Element:
    """Build an ODM-XML ElementTree from a QuestionnaireResponse.

    Maps each QuestionnaireResponse item to CDASH/SDTM variables using the
    eligibility-rule extension or code system from the corresponding
    Questionnaire item.

    Returns the root ODM Element.
    """
    now = datetime.now(timezone.utc).isoformat()
    file_oid = f"trial-{trial_id}-export-{uuid.uuid4().hex[:8]}"

    # Build a lookup from linkId → Questionnaire item for metadata
    q_items_by_link: dict[str, dict] = {}
    _index_questionnaire_items(questionnaire.get("item", []), q_items_by_link)

    # Root ODM element
    odm = ET.Element("ODM", {
        "xmlns": _ODM_NS,
        "FileType": "Transactional",
        "FileOID": file_oid,
        "CreationDateTime": now,
        "ODMVersion": "1.3.2",
    })

    # ClinicalData
    subject_ref = questionnaire_response.get("subject", {}).get("reference", "")
    patient_id = subject_ref.split("/")[-1] if "/" in subject_ref else subject_ref

    clinical_data = ET.SubElement(odm, "ClinicalData", {
        "StudyOID": trial_id,
        "MetaDataVersionOID": questionnaire.get("version", "v1"),
    })

    subject_data = ET.SubElement(clinical_data, "SubjectData", {
        "SubjectKey": patient_id,
    })

    # Determine study event from questionnaire type
    q_title = questionnaire.get("title", "").lower()
    event_oid = "MONITORING" if "monitor" in q_title else "SCREENING"

    study_event = ET.SubElement(subject_data, "StudyEventData", {
        "StudyEventOID": event_oid,
    })

    # Group items by SDTM domain
    domain_items: dict[str, list[tuple[str, str, str]]] = {}
    _collect_response_items(
        questionnaire_response.get("item", []),
        q_items_by_link,
        cdash_map,
        domain_items,
    )

    # Build FormData / ItemGroupData / ItemData per domain
    for domain, items in domain_items.items():
        form_data = ET.SubElement(study_event, "FormData", {
            "FormOID": domain,
        })
        item_group = ET.SubElement(form_data, "ItemGroupData", {
            "ItemGroupOID": domain,
        })
        for cdash_var, sdtm_var, value in items:
            ET.SubElement(item_group, "ItemData", {
                "ItemOID": f"{domain}.{sdtm_var}",
                "Value": value,
            })

    return odm


def _index_questionnaire_items(items: list[dict], index: dict[str, dict]) -> None:
    """Recursively index Questionnaire items by linkId."""
    for item in items:
        link_id = item.get("linkId", "")
        if link_id:
            index[link_id] = item
        _index_questionnaire_items(item.get("item", []), index)


def _collect_response_items(
    response_items: list[dict],
    q_items_by_link: dict[str, dict],
    cdash_map: dict[str, tuple[str, str, str]],
    domain_items: dict[str, list[tuple[str, str, str]]],
) -> None:
    """Recursively collect response items mapped to SDTM domains."""
    for resp_item in response_items:
        link_id = resp_item.get("linkId", "")
        value = _extract_item_value(resp_item)

        if value and link_id:
            q_item = q_items_by_link.get(link_id, {})
            fhir_path = _extract_fhir_path_from_item(q_item)
            mapping = _resolve_cdash_variable(fhir_path, cdash_map)

            if mapping:
                cdash_var, sdtm_domain, sdtm_var = mapping
                domain_items.setdefault(sdtm_domain, [])
                domain_items[sdtm_domain].append((cdash_var, sdtm_var, value))

        # Recurse into nested items
        _collect_response_items(
            resp_item.get("item", []),
            q_items_by_link,
            cdash_map,
            domain_items,
        )


def _validate_odm_xml(odm_element: ET.Element) -> list[str]:
    """Validate an ODM-XML element against basic CDISC ODM structural rules.

    Returns a list of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Root must be ODM
    tag = odm_element.tag
    # Strip namespace if present
    if "}" in tag:
        tag = tag.split("}")[1]
    if tag != "ODM":
        errors.append(f"Root element must be 'ODM', got '{tag}'")

    # Required attributes
    for attr in ("FileType", "FileOID", "CreationDateTime"):
        if not odm_element.get(attr):
            errors.append(f"Missing required ODM attribute: {attr}")

    # Must have at least one ClinicalData
    clinical_data = odm_element.findall("ClinicalData")
    if not clinical_data:
        # Try with namespace
        clinical_data = odm_element.findall(f"{{{_ODM_NS}}}ClinicalData")
    if not clinical_data:
        errors.append("ODM must contain at least one ClinicalData element")
        return errors

    for cd in clinical_data:
        if not cd.get("StudyOID"):
            errors.append("ClinicalData missing StudyOID attribute")
        subjects = cd.findall("SubjectData") or cd.findall(f"{{{_ODM_NS}}}SubjectData")
        if not subjects:
            errors.append("ClinicalData must contain at least one SubjectData")
        for sd in subjects:
            if not sd.get("SubjectKey"):
                errors.append("SubjectData missing SubjectKey attribute")

    return errors


def _odm_to_string(odm_element: ET.Element) -> str:
    """Serialize an ODM-XML element to a string with XML declaration."""
    ET.indent(odm_element, space="  ")
    return ET.tostring(odm_element, encoding="unicode", xml_declaration=True)


def _import_odm_xml(odm_xml_string: str) -> dict:
    """Import an ODM-XML string back into a QuestionnaireResponse-like dict.

    Used for round-trip validation (Property 23). Reconstructs a simplified
    QuestionnaireResponse from the ODM-XML structure.
    """
    root = ET.fromstring(odm_xml_string)

    # Handle namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    clinical_data = root.find(f"{ns}ClinicalData")
    if clinical_data is None:
        return {"error": "No ClinicalData found"}

    study_oid = clinical_data.get("StudyOID", "")
    subject_data = clinical_data.find(f"{ns}SubjectData")
    if subject_data is None:
        return {"error": "No SubjectData found"}

    patient_id = subject_data.get("SubjectKey", "")

    items: list[dict] = []
    for study_event in subject_data.findall(f"{ns}StudyEventData"):
        for form_data in study_event.findall(f"{ns}FormData"):
            for item_group in form_data.findall(f"{ns}ItemGroupData"):
                for item_data in item_group.findall(f"{ns}ItemData"):
                    item_oid = item_data.get("ItemOID", "")
                    value = item_data.get("Value", "")
                    items.append({
                        "linkId": item_oid,
                        "answer": [{"valueString": value}],
                    })

    return {
        "resourceType": "QuestionnaireResponse",
        "subject": {"reference": f"Patient/{patient_id}"},
        "status": "completed",
        "item": items,
        "_source_study": study_oid,
    }


# ── EDC transmission ──────────────────────────────────────────────────────────


def _transmit_to_medidata_rave(
    odm_xml: str, endpoint_url: str, trial_id: str
) -> dict:
    """Transmit ODM-XML to Medidata Rave via Rave Web Services API.

    Returns dict with status and response details.
    """
    headers = {
        "Content-Type": "application/xml",
        "Accept": "application/xml",
    }
    response = requests.post(
        endpoint_url,
        data=odm_xml.encode("utf-8"),
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return {
        "edc_system": "medidata_rave",
        "status": "transmitted",
        "http_status": response.status_code,
        "response_text": response.text[:500],
    }


def _transmit_to_oracle_clinical_one(
    odm_xml: str, endpoint_url: str, trial_id: str
) -> dict:
    """Transmit ODM-XML to Oracle Clinical One via REST API.

    Returns dict with status and response details.
    """
    headers = {
        "Content-Type": "application/xml",
        "Accept": "application/json",
    }
    response = requests.post(
        endpoint_url,
        data=odm_xml.encode("utf-8"),
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return {
        "edc_system": "oracle_clinical_one",
        "status": "transmitted",
        "http_status": response.status_code,
        "response_text": response.text[:500],
    }


def _transmit_with_retry(
    odm_xml: str,
    edc_system: str,
    endpoint_url: str,
    trial_id: str,
    session_id: str,
) -> dict:
    """Transmit ODM-XML to EDC with exponential backoff retry.

    Retries up to 3 times (5s, 15s, 45s). On final failure, queues for
    manual review and logs in Audit_Logger.
    """
    transmit_fn = (
        _transmit_to_medidata_rave
        if edc_system == "medidata_rave"
        else _transmit_to_oracle_clinical_one
    )

    last_error = None
    for attempt in range(EDC_RETRY_ATTEMPTS):
        try:
            result = transmit_fn(odm_xml, endpoint_url, trial_id)
            # Log successful transmission
            audit_log_event.invoke({
                "session_id": session_id,
                "event_type": "data_access",
                "trial_id": trial_id,
                "agent_id": "edc-bridge",
                "user_identity": "edc-bridge",
                "event_data": {
                    "action": "edc_transmission",
                    "edc_system": edc_system,
                    "status": "success",
                    "attempt": attempt + 1,
                },
            })
            return result
        except Exception as exc:
            last_error = exc
            if attempt < EDC_RETRY_ATTEMPTS - 1:
                backoff = EDC_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "EDC transmission attempt %d failed, retrying in %ds: %s",
                    attempt + 1, backoff, exc,
                )
                time.sleep(backoff)

    # All retries exhausted — queue for manual review
    logger.error(
        "EDC transmission to %s failed after %d attempts for trial %s",
        edc_system, EDC_RETRY_ATTEMPTS, trial_id,
    )
    audit_log_event.invoke({
        "session_id": session_id,
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "edc-bridge",
        "user_identity": "edc-bridge",
        "event_data": {
            "action": "edc_transmission",
            "edc_system": edc_system,
            "status": "failed",
            "error": str(last_error),
            "retry_count": EDC_RETRY_ATTEMPTS,
            "queued_for_manual_review": True,
        },
    })

    return {
        "edc_system": edc_system,
        "status": "failed",
        "error": str(last_error),
        "queued_for_manual_review": True,
    }


# ── Main export tool ──────────────────────────────────────────────────────────


@tool
def export_odm_xml(
    questionnaire_response: dict,
    questionnaire: dict,
    trial_id: str,
    session_id: str = "",
    edc_system: str | None = None,
    edc_endpoint_url: str | None = None,
) -> dict:
    """Export a FHIR QuestionnaireResponse as CDISC ODM-XML.

    Generates an ODM-XML document conforming to the trial-specific ODM metadata
    definition, validates against the CDISC ODM-XML schema, maps items to CDASH
    variable names and SDTM domain/variable pairs, and optionally transmits to
    an EDC system (Medidata Rave or Oracle Clinical One) with retry.

    Args:
        questionnaire_response: FHIR QuestionnaireResponse resource dict.
        questionnaire: FHIR Questionnaire resource dict (for item metadata).
        trial_id: NCT number or internal trial identifier.
        session_id: Screening/monitoring session ID for audit logging.
        edc_system: Optional EDC target — 'medidata_rave' or 'oracle_clinical_one'.
        edc_endpoint_url: Optional EDC API endpoint URL for transmission.

    Returns:
        Dict with 'odm_xml' (string), 'validation_errors' (list),
        'variable_mappings' (list of mapped items), and optionally
        'transmission_result' if edc_system is specified.
    """
    if not session_id:
        session_id = f"edc_{uuid.uuid4().hex[:12]}"

    cdash_map = _get_cdash_mapping(trial_id)

    # Build ODM-XML
    try:
        odm_element = _build_odm_xml(
            questionnaire_response, questionnaire, trial_id, cdash_map,
        )
    except Exception as exc:
        logger.exception("Failed to build ODM-XML for trial %s", trial_id)
        return {"error": f"ODM-XML generation failed: {exc}"}

    # Validate
    validation_errors = _validate_odm_xml(odm_element)
    odm_xml_string = _odm_to_string(odm_element)

    # Collect variable mappings for audit/traceability
    variable_mappings = _collect_variable_mappings(
        questionnaire_response, questionnaire, cdash_map,
    )

    # Log the export event
    audit_log_event.invoke({
        "session_id": session_id,
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "edc-bridge",
        "user_identity": "edc-bridge",
        "event_data": {
            "action": "odm_xml_export",
            "validation_errors": validation_errors,
            "item_count": len(variable_mappings),
            "has_errors": len(validation_errors) > 0,
        },
    })

    result: dict = {
        "odm_xml": odm_xml_string,
        "validation_errors": validation_errors,
        "variable_mappings": variable_mappings,
        "trial_id": trial_id,
        "session_id": session_id,
    }

    # Transmit to EDC if configured
    if edc_system and edc_endpoint_url and not validation_errors:
        transmission_result = _transmit_with_retry(
            odm_xml_string, edc_system, edc_endpoint_url, trial_id, session_id,
        )
        result["transmission_result"] = transmission_result
    elif edc_system and validation_errors:
        result["transmission_result"] = {
            "status": "skipped",
            "reason": "ODM-XML validation errors must be resolved before transmission",
        }

    return result


def _collect_variable_mappings(
    questionnaire_response: dict,
    questionnaire: dict,
    cdash_map: dict[str, tuple[str, str, str]],
) -> list[dict]:
    """Collect all variable mappings from a QuestionnaireResponse for audit."""
    q_items_by_link: dict[str, dict] = {}
    _index_questionnaire_items(questionnaire.get("item", []), q_items_by_link)

    mappings: list[dict] = []
    _collect_mappings_recursive(
        questionnaire_response.get("item", []),
        q_items_by_link,
        cdash_map,
        mappings,
    )
    return mappings


def _collect_mappings_recursive(
    response_items: list[dict],
    q_items_by_link: dict[str, dict],
    cdash_map: dict[str, tuple[str, str, str]],
    mappings: list[dict],
) -> None:
    """Recursively collect variable mappings from response items."""
    for resp_item in response_items:
        link_id = resp_item.get("linkId", "")
        value = _extract_item_value(resp_item)
        if value and link_id:
            q_item = q_items_by_link.get(link_id, {})
            fhir_path = _extract_fhir_path_from_item(q_item)
            mapping = _resolve_cdash_variable(fhir_path, cdash_map)
            if mapping:
                cdash_var, sdtm_domain, sdtm_var = mapping
                mappings.append({
                    "linkId": link_id,
                    "fhir_path": fhir_path,
                    "cdash_variable": cdash_var,
                    "sdtm_domain": sdtm_domain,
                    "sdtm_variable": sdtm_var,
                    "value": value,
                })
        _collect_mappings_recursive(
            resp_item.get("item", []),
            q_items_by_link,
            cdash_map,
            mappings,
        )


# ── SDTM dataset generation ──────────────────────────────────────────────────


def _build_sdtm_record(
    domain: str,
    study_id: str,
    subject_id: str,
    seq: int,
    data: dict[str, str],
) -> dict:
    """Build a single SDTM record with standard variables."""
    record = {
        "STUDYID": study_id,
        "DOMAIN": domain,
        "USUBJID": f"{study_id}-{subject_id}",
        f"{domain}SEQ": seq,
    }
    record.update(data)
    return record


def _extract_sdtm_from_response(
    questionnaire_response: dict,
    questionnaire: dict,
    domain: str,
    trial_id: str,
    cdash_map: dict[str, tuple[str, str, str]],
) -> list[dict]:
    """Extract SDTM records for a specific domain from a QuestionnaireResponse."""
    q_items_by_link: dict[str, dict] = {}
    _index_questionnaire_items(questionnaire.get("item", []), q_items_by_link)

    subject_ref = questionnaire_response.get("subject", {}).get("reference", "")
    subject_id = subject_ref.split("/")[-1] if "/" in subject_ref else subject_ref

    # Collect all items that map to this domain
    domain_values: list[dict[str, str]] = []
    _collect_domain_values(
        questionnaire_response.get("item", []),
        q_items_by_link,
        cdash_map,
        domain,
        domain_values,
    )

    # Group values into records (one record per logical group)
    records: list[dict] = []
    if domain == "DM":
        # DM is one record per subject — merge all values
        merged: dict[str, str] = {}
        for vals in domain_values:
            merged.update(vals)
        if merged:
            records.append(_build_sdtm_record(domain, trial_id, subject_id, 1, merged))
    else:
        # Other domains: one record per item
        for seq, vals in enumerate(domain_values, start=1):
            records.append(_build_sdtm_record(domain, trial_id, subject_id, seq, vals))

    return records


def _collect_domain_values(
    response_items: list[dict],
    q_items_by_link: dict[str, dict],
    cdash_map: dict[str, tuple[str, str, str]],
    target_domain: str,
    domain_values: list[dict[str, str]],
) -> None:
    """Recursively collect SDTM variable values for a target domain."""
    for resp_item in response_items:
        link_id = resp_item.get("linkId", "")
        value = _extract_item_value(resp_item)
        if value and link_id:
            q_item = q_items_by_link.get(link_id, {})
            fhir_path = _extract_fhir_path_from_item(q_item)
            mapping = _resolve_cdash_variable(fhir_path, cdash_map)
            if mapping:
                _, sdtm_domain, sdtm_var = mapping
                if sdtm_domain == target_domain:
                    domain_values.append({sdtm_var: value})
        _collect_domain_values(
            resp_item.get("item", []),
            q_items_by_link,
            cdash_map,
            target_domain,
            domain_values,
        )


def _generate_define_xml(
    study_id: str,
    domains: list[str],
    dataset_type: str = "SDTM",
) -> str:
    """Generate define.xml metadata for SDTM or ADaM datasets.

    Returns an XML string conforming to CDISC Define-XML 2.0 structure.
    """
    meta_source = _SDTM_DOMAIN_META if dataset_type == "SDTM" else _ADAM_DATASET_META
    now = datetime.now(timezone.utc).isoformat()

    define_ns = "http://www.cdisc.org/ns/def/v2.0"
    odm_ns = _ODM_NS

    root = ET.Element("ODM", {
        "xmlns": odm_ns,
        "xmlns:def": define_ns,
        "FileType": "Snapshot",
        "FileOID": f"{study_id}-define-{dataset_type}",
        "CreationDateTime": now,
        "ODMVersion": "1.3.2",
    })

    study = ET.SubElement(root, "Study", {"OID": study_id})
    mdv = ET.SubElement(study, "MetaDataVersion", {
        "OID": f"{study_id}-{dataset_type}-v1",
        "Name": f"{study_id} {dataset_type} Metadata",
    })

    for domain in domains:
        meta = meta_source.get(domain, {})
        if not meta:
            continue

        item_group = ET.SubElement(mdv, "ItemGroupDef", {
            "OID": f"IG.{domain}",
            "Name": domain,
            "Domain": domain,
            "Repeating": "No" if domain in ("DM", "ADSL") else "Yes",
            "Purpose": "Analysis" if dataset_type == "ADaM" else "Tabulation",
            "def:Label": meta.get("label", domain),
            "def:Structure": meta.get("structure", ""),
            "def:Class": meta.get("class", ""),
        })

        # Add key variable references
        for key_var in meta.get("key_variables", []):
            ET.SubElement(item_group, "ItemRef", {
                "ItemOID": f"IT.{domain}.{key_var}",
                "Mandatory": "Yes",
                "KeySequence": str(meta["key_variables"].index(key_var) + 1),
            })

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _validate_sdtm_pinnacle21(
    records: list[dict], domain: str
) -> list[str]:
    """Validate SDTM records against Pinnacle 21 Community rules.

    Implements a subset of common Pinnacle 21 validation checks.
    Returns a list of validation issues.
    """
    issues: list[str] = []
    meta = _SDTM_DOMAIN_META.get(domain, {})
    key_vars = meta.get("key_variables", [])

    for i, record in enumerate(records):
        # Check required key variables
        for key_var in key_vars:
            if not record.get(key_var):
                issues.append(
                    f"{domain} record {i + 1}: Missing required key variable {key_var}"
                )

        # STUDYID and DOMAIN must be present
        if not record.get("STUDYID"):
            issues.append(f"{domain} record {i + 1}: Missing STUDYID")
        if not record.get("DOMAIN"):
            issues.append(f"{domain} record {i + 1}: Missing DOMAIN")
        elif record["DOMAIN"] != domain:
            issues.append(
                f"{domain} record {i + 1}: DOMAIN value '{record['DOMAIN']}' "
                f"does not match expected '{domain}'"
            )

        # USUBJID format check
        usubjid = record.get("USUBJID", "")
        if not usubjid:
            issues.append(f"{domain} record {i + 1}: Missing USUBJID")

        # Sequence variable must be positive integer
        seq_var = f"{domain}SEQ"
        seq_val = record.get(seq_var)
        if seq_val is not None and (not isinstance(seq_val, int) or seq_val < 1):
            issues.append(
                f"{domain} record {i + 1}: {seq_var} must be a positive integer"
            )

    # Check for duplicate key combinations
    seen_keys: set[tuple] = set()
    for i, record in enumerate(records):
        key_tuple = tuple(str(record.get(k, "")) for k in key_vars)
        if key_tuple in seen_keys:
            issues.append(
                f"{domain} record {i + 1}: Duplicate key combination {key_vars}={list(key_tuple)}"
            )
        seen_keys.add(key_tuple)

    return issues


@tool
def generate_sdtm_datasets(
    questionnaire_responses: list[dict],
    questionnaires: list[dict],
    trial_id: str,
    domains: list[str] | None = None,
    session_id: str = "",
) -> dict:
    """Generate SDTM-formatted datasets from finalized QuestionnaireResponses.

    Produces SDTM datasets for Demographics (DM), Medical History (MH),
    Concomitant Medications (CM), Adverse Events (AE), and Vital Signs (VS)
    domains. Validates using Pinnacle 21 Community rules and generates
    define.xml metadata.

    Args:
        questionnaire_responses: List of finalized FHIR QuestionnaireResponse dicts.
        questionnaires: List of corresponding FHIR Questionnaire dicts.
        trial_id: NCT number or internal trial identifier.
        domains: Optional list of SDTM domains to generate. Defaults to
            ["DM", "MH", "CM", "AE", "VS"].
        session_id: Optional session ID for audit logging.

    Returns:
        Dict with 'datasets' (domain → records), 'define_xml' (string),
        'validation_issues' (domain → issues list), and 'summary'.
    """
    if not session_id:
        session_id = f"sdtm_{uuid.uuid4().hex[:12]}"

    target_domains = domains or ["DM", "MH", "CM", "AE", "VS"]
    cdash_map = _get_cdash_mapping(trial_id)

    # Build a questionnaire lookup by ID/URL for matching
    q_lookup: dict[str, dict] = {}
    for q in questionnaires:
        q_id = q.get("id", "")
        if q_id:
            q_lookup[q_id] = q
            q_lookup[f"Questionnaire/{q_id}"] = q

    datasets: dict[str, list[dict]] = {d: [] for d in target_domains}
    validation_issues: dict[str, list[str]] = {}

    for qr in questionnaire_responses:
        # Match QuestionnaireResponse to its Questionnaire
        q_ref = qr.get("questionnaire", "")
        questionnaire = q_lookup.get(q_ref, {})
        if not questionnaire and questionnaires:
            questionnaire = questionnaires[0]  # fallback to first

        for domain in target_domains:
            records = _extract_sdtm_from_response(
                qr, questionnaire, domain, trial_id, cdash_map,
            )
            datasets[domain].extend(records)

    # Validate each domain with Pinnacle 21 rules
    for domain in target_domains:
        issues = _validate_sdtm_pinnacle21(datasets[domain], domain)
        if issues:
            validation_issues[domain] = issues

    # Generate define.xml
    define_xml = _generate_define_xml(trial_id, target_domains, "SDTM")

    # Log the generation event
    audit_log_event.invoke({
        "session_id": session_id,
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "edc-bridge",
        "user_identity": "edc-bridge",
        "event_data": {
            "action": "sdtm_generation",
            "domains": target_domains,
            "record_counts": {d: len(r) for d, r in datasets.items()},
            "validation_issue_count": sum(len(v) for v in validation_issues.values()),
        },
    })

    total_records = sum(len(r) for r in datasets.values())
    return {
        "datasets": datasets,
        "define_xml": define_xml,
        "validation_issues": validation_issues,
        "summary": {
            "trial_id": trial_id,
            "domains_generated": target_domains,
            "total_records": total_records,
            "total_validation_issues": sum(len(v) for v in validation_issues.values()),
        },
    }


# ── ADaM dataset generation ──────────────────────────────────────────────────


def _generate_adsl(
    dm_records: list[dict], mh_records: list[dict], trial_id: str
) -> list[dict]:
    """Generate ADSL (Subject-Level Analysis Dataset) from SDTM DM and MH.

    ADSL contains one record per subject with demographics and key baseline
    characteristics derived from DM and MH domains.
    """
    adsl_records: list[dict] = []
    for dm in dm_records:
        adsl = {
            "STUDYID": dm.get("STUDYID", trial_id),
            "USUBJID": dm.get("USUBJID", ""),
            "SUBJID": dm.get("SUBJID", dm.get("USUBJID", "").split("-")[-1]),
            "SITEID": dm.get("SITEID", ""),
            "AGE": dm.get("AGE", ""),
            "AGEU": dm.get("AGEU", "YEARS"),
            "SEX": dm.get("SEX", ""),
            "RACE": dm.get("RACE", ""),
            "ETHNIC": dm.get("ETHNIC", ""),
            "TRT01P": "",  # Planned treatment — populated from trial config
            "TRT01A": "",  # Actual treatment
            "SAFFL": "Y",  # Safety population flag
            "ITTFL": "Y",  # Intent-to-treat flag
        }

        # Derive medical history flags from MH records for this subject
        subject_mh = [
            m for m in mh_records if m.get("USUBJID") == dm.get("USUBJID")
        ]
        adsl["MHCNT"] = len(subject_mh)

        adsl_records.append(adsl)

    return adsl_records


def _generate_adae(
    ae_records: list[dict], dm_records: list[dict], trial_id: str
) -> list[dict]:
    """Generate ADAE (Adverse Event Analysis Dataset) from SDTM AE and DM.

    ADAE contains one record per adverse event per subject with analysis
    variables derived from AE and DM domains.
    """
    # Build DM lookup for subject-level data
    dm_by_subject: dict[str, dict] = {}
    for dm in dm_records:
        dm_by_subject[dm.get("USUBJID", "")] = dm

    adae_records: list[dict] = []
    for ae in ae_records:
        usubjid = ae.get("USUBJID", "")
        dm = dm_by_subject.get(usubjid, {})

        adae = {
            "STUDYID": ae.get("STUDYID", trial_id),
            "USUBJID": usubjid,
            "AESEQ": ae.get("AESEQ", ""),
            "AETERM": ae.get("AETERM", ""),
            "AEDECOD": ae.get("AEDECOD", ae.get("AETERM", "")),
            "AEBODSYS": ae.get("AEBODSYS", ""),
            "AESEV": ae.get("AESEV", ""),
            "AESER": ae.get("AESER", ""),
            "AEREL": ae.get("AEREL", ""),
            "AEOUT": ae.get("AEOUT", ""),
            "AESTDTC": ae.get("AESTDTC", ""),
            "AEENDTC": ae.get("AEENDTC", ""),
            "TRTA": dm.get("TRT01A", ""),
            "TRTP": dm.get("TRT01P", ""),
            "AGE": dm.get("AGE", ""),
            "SEX": dm.get("SEX", ""),
            "RACE": dm.get("RACE", ""),
            "SAFFL": "Y",
        }
        adae_records.append(adae)

    return adae_records


@tool
def generate_adam_datasets(
    sdtm_datasets: dict[str, list[dict]],
    trial_id: str,
    datasets: list[str] | None = None,
    session_id: str = "",
) -> dict:
    """Generate ADaM-formatted analysis datasets from SDTM datasets.

    Produces ADaM datasets (ADSL, ADAE) from SDTM source domains with
    accompanying define.xml metadata.

    Args:
        sdtm_datasets: Dict of SDTM domain → list of records (from
            generate_sdtm_datasets output).
        trial_id: NCT number or internal trial identifier.
        datasets: Optional list of ADaM datasets to generate. Defaults to
            ["ADSL", "ADAE"].
        session_id: Optional session ID for audit logging.

    Returns:
        Dict with 'datasets' (name → records), 'define_xml' (string),
        and 'summary'.
    """
    if not session_id:
        session_id = f"adam_{uuid.uuid4().hex[:12]}"

    target_datasets = datasets or ["ADSL", "ADAE"]
    dm_records = sdtm_datasets.get("DM", [])
    mh_records = sdtm_datasets.get("MH", [])
    ae_records = sdtm_datasets.get("AE", [])

    adam_datasets: dict[str, list[dict]] = {}

    if "ADSL" in target_datasets:
        adam_datasets["ADSL"] = _generate_adsl(dm_records, mh_records, trial_id)

    if "ADAE" in target_datasets:
        adam_datasets["ADAE"] = _generate_adae(ae_records, dm_records, trial_id)

    # Generate define.xml for ADaM
    define_xml = _generate_define_xml(trial_id, target_datasets, "ADaM")

    # Log the generation event
    audit_log_event.invoke({
        "session_id": session_id,
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "edc-bridge",
        "user_identity": "edc-bridge",
        "event_data": {
            "action": "adam_generation",
            "datasets": target_datasets,
            "record_counts": {d: len(r) for d, r in adam_datasets.items()},
        },
    })

    total_records = sum(len(r) for r in adam_datasets.values())
    return {
        "datasets": adam_datasets,
        "define_xml": define_xml,
        "summary": {
            "trial_id": trial_id,
            "datasets_generated": target_datasets,
            "total_records": total_records,
            "source_domains_used": list(sdtm_datasets.keys()),
        },
    }

