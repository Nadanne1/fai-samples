"""Terminology mapping service for clinical trial data standardization.

Maps clinical terms across standard terminologies: MedDRA, SNOMED CT, ICD-10,
LOINC, RxNorm, and WHO Drug Dictionary. Returns mappings with confidence scores
and flags terms below 0.8 confidence for manual review. Supports versioned
terminology releases with per-trial configuration from TrialProtocolConfig.
"""

import logging
from datetime import datetime, timezone

import boto3
from langchain_core.tools import tool

from components.trial_config_store import get_trial_config
from config.settings import AWS_REGION
from tools.audit import audit_log_event

logger = logging.getLogger(__name__)

# Manual-review confidence threshold
CONFIDENCE_THRESHOLD = 0.8

# Supported terminology systems with their canonical URIs
TERMINOLOGY_SYSTEMS = {
    "meddra": {
        "uri": "http://terminology.hl7.org/CodeSystem/mdr",
        "display": "MedDRA",
    },
    "snomed": {
        "uri": "http://snomed.info/sct",
        "display": "SNOMED CT",
    },
    "icd10": {
        "uri": "http://hl7.org/fhir/sid/icd-10",
        "display": "ICD-10",
    },
    "loinc": {
        "uri": "http://loinc.org",
        "display": "LOINC",
    },
    "rxnorm": {
        "uri": "http://www.nlm.nih.gov/research/umls/rxnorm",
        "display": "RxNorm",
    },
    "who_drug": {
        "uri": "http://who.int/drug-dictionary",
        "display": "WHO Drug Dictionary",
    },
}

# Default terminology versions when no trial-specific config exists
DEFAULT_TERMINOLOGY_VERSIONS = {
    "meddra": "26.0",
    "snomed": "2024-03",
    "icd10": "2024",
    "loinc": "2.77",
    "rxnorm": "2024-03",
    "who_drug": "2024-Q1",
}

# ── Curated lookup tables ────────────────────────────────────────────────
# In a production system these would be backed by a full terminology server
# (e.g. HAPI FHIR Terminology, AWS HealthLake $translate). For this PoC we
# use representative lookup dicts that cover the most common clinical-trial
# terms and demonstrate the mapping + confidence-scoring logic.

_CONDITION_SNOMED: dict[str, dict] = {
    "type 2 diabetes": {"code": "44054006", "display": "Type 2 diabetes mellitus", "confidence": 0.95},
    "diabetes mellitus type 2": {"code": "44054006", "display": "Type 2 diabetes mellitus", "confidence": 0.95},
    "hypertension": {"code": "38341003", "display": "Hypertensive disorder", "confidence": 0.95},
    "high blood pressure": {"code": "38341003", "display": "Hypertensive disorder", "confidence": 0.85},
    "asthma": {"code": "195967001", "display": "Asthma", "confidence": 0.97},
    "breast cancer": {"code": "254837009", "display": "Malignant neoplasm of breast", "confidence": 0.93},
    "lung cancer": {"code": "93880001", "display": "Primary malignant neoplasm of lung", "confidence": 0.93},
    "chronic kidney disease": {"code": "709044004", "display": "Chronic kidney disease", "confidence": 0.94},
    "heart failure": {"code": "84114007", "display": "Heart failure", "confidence": 0.95},
    "atrial fibrillation": {"code": "49436004", "display": "Atrial fibrillation", "confidence": 0.96},
    "copd": {"code": "13645005", "display": "Chronic obstructive lung disease", "confidence": 0.94},
    "depression": {"code": "35489007", "display": "Depressive disorder", "confidence": 0.88},
    "anxiety": {"code": "197480006", "display": "Anxiety disorder", "confidence": 0.87},
    "obesity": {"code": "414916001", "display": "Obesity", "confidence": 0.96},
    "rheumatoid arthritis": {"code": "69896004", "display": "Rheumatoid arthritis", "confidence": 0.95},
}

_CONDITION_ICD10: dict[str, dict] = {
    "type 2 diabetes": {"code": "E11", "display": "Type 2 diabetes mellitus", "confidence": 0.95},
    "diabetes mellitus type 2": {"code": "E11", "display": "Type 2 diabetes mellitus", "confidence": 0.95},
    "hypertension": {"code": "I10", "display": "Essential hypertension", "confidence": 0.94},
    "high blood pressure": {"code": "I10", "display": "Essential hypertension", "confidence": 0.82},
    "asthma": {"code": "J45", "display": "Asthma", "confidence": 0.96},
    "breast cancer": {"code": "C50", "display": "Malignant neoplasm of breast", "confidence": 0.93},
    "lung cancer": {"code": "C34", "display": "Malignant neoplasm of bronchus and lung", "confidence": 0.92},
    "chronic kidney disease": {"code": "N18", "display": "Chronic kidney disease", "confidence": 0.94},
    "heart failure": {"code": "I50", "display": "Heart failure", "confidence": 0.95},
    "atrial fibrillation": {"code": "I48", "display": "Atrial fibrillation and flutter", "confidence": 0.94},
    "copd": {"code": "J44", "display": "Other chronic obstructive pulmonary disease", "confidence": 0.93},
    "depression": {"code": "F32", "display": "Depressive episode", "confidence": 0.86},
    "anxiety": {"code": "F41", "display": "Other anxiety disorders", "confidence": 0.85},
    "obesity": {"code": "E66", "display": "Overweight and obesity", "confidence": 0.95},
    "rheumatoid arthritis": {"code": "M05", "display": "Rheumatoid arthritis", "confidence": 0.94},
}

_MEDICATION_RXNORM: dict[str, dict] = {
    "metformin": {"code": "6809", "display": "metformin", "confidence": 0.97},
    "lisinopril": {"code": "29046", "display": "lisinopril", "confidence": 0.97},
    "amlodipine": {"code": "17767", "display": "amlodipine", "confidence": 0.97},
    "atorvastatin": {"code": "83367", "display": "atorvastatin", "confidence": 0.97},
    "omeprazole": {"code": "7646", "display": "omeprazole", "confidence": 0.97},
    "aspirin": {"code": "1191", "display": "aspirin", "confidence": 0.97},
    "warfarin": {"code": "11289", "display": "warfarin", "confidence": 0.96},
    "insulin": {"code": "5856", "display": "insulin", "confidence": 0.90},
    "prednisone": {"code": "8640", "display": "prednisone", "confidence": 0.96},
    "ibuprofen": {"code": "5640", "display": "ibuprofen", "confidence": 0.97},
}

_MEDICATION_WHO_DRUG: dict[str, dict] = {
    "metformin": {"code": "002440", "display": "metformin", "confidence": 0.95},
    "lisinopril": {"code": "007330", "display": "lisinopril", "confidence": 0.95},
    "amlodipine": {"code": "009690", "display": "amlodipine", "confidence": 0.95},
    "atorvastatin": {"code": "010070", "display": "atorvastatin", "confidence": 0.95},
    "omeprazole": {"code": "008680", "display": "omeprazole", "confidence": 0.95},
    "aspirin": {"code": "000153", "display": "acetylsalicylic acid", "confidence": 0.93},
    "warfarin": {"code": "002620", "display": "warfarin", "confidence": 0.94},
    "insulin": {"code": "001050", "display": "insulin", "confidence": 0.88},
    "prednisone": {"code": "001810", "display": "prednisone", "confidence": 0.94},
    "ibuprofen": {"code": "005290", "display": "ibuprofen", "confidence": 0.95},
}

_AE_MEDDRA: dict[str, dict] = {
    "headache": {"pt": "Headache", "pt_code": "10019211", "soc": "Nervous system disorders", "soc_code": "10029205", "confidence": 0.97},
    "nausea": {"pt": "Nausea", "pt_code": "10028813", "soc": "Gastrointestinal disorders", "soc_code": "10017947", "confidence": 0.97},
    "vomiting": {"pt": "Vomiting", "pt_code": "10047700", "soc": "Gastrointestinal disorders", "soc_code": "10017947", "confidence": 0.97},
    "dizziness": {"pt": "Dizziness", "pt_code": "10013573", "soc": "Nervous system disorders", "soc_code": "10029205", "confidence": 0.96},
    "fatigue": {"pt": "Fatigue", "pt_code": "10016256", "soc": "General disorders", "soc_code": "10018065", "confidence": 0.95},
    "rash": {"pt": "Rash", "pt_code": "10037844", "soc": "Skin disorders", "soc_code": "10040785", "confidence": 0.94},
    "diarrhea": {"pt": "Diarrhoea", "pt_code": "10012735", "soc": "Gastrointestinal disorders", "soc_code": "10017947", "confidence": 0.96},
    "cough": {"pt": "Cough", "pt_code": "10011224", "soc": "Respiratory disorders", "soc_code": "10038738", "confidence": 0.96},
    "insomnia": {"pt": "Insomnia", "pt_code": "10022437", "soc": "Psychiatric disorders", "soc_code": "10037175", "confidence": 0.95},
    "back pain": {"pt": "Back pain", "pt_code": "10003988", "soc": "Musculoskeletal disorders", "soc_code": "10028395", "confidence": 0.94},
    "chest pain": {"pt": "Chest pain", "pt_code": "10008479", "soc": "General disorders", "soc_code": "10018065", "confidence": 0.90},
    "dyspnea": {"pt": "Dyspnoea", "pt_code": "10013968", "soc": "Respiratory disorders", "soc_code": "10038738", "confidence": 0.96},
    "myalgia": {"pt": "Myalgia", "pt_code": "10028411", "soc": "Musculoskeletal disorders", "soc_code": "10028395", "confidence": 0.95},
}

_LAB_LOINC: dict[str, dict] = {
    "hemoglobin a1c": {"code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood", "confidence": 0.96},
    "hba1c": {"code": "4548-4", "display": "Hemoglobin A1c/Hemoglobin.total in Blood", "confidence": 0.94},
    "fasting glucose": {"code": "1558-6", "display": "Fasting glucose [Mass/volume] in Serum or Plasma", "confidence": 0.95},
    "creatinine": {"code": "2160-0", "display": "Creatinine [Mass/volume] in Serum or Plasma", "confidence": 0.96},
    "egfr": {"code": "33914-3", "display": "Glomerular filtration rate/1.73 sq M.predicted", "confidence": 0.93},
    "alt": {"code": "1742-6", "display": "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "confidence": 0.95},
    "ast": {"code": "1920-8", "display": "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "confidence": 0.95},
    "total cholesterol": {"code": "2093-3", "display": "Cholesterol [Mass/volume] in Serum or Plasma", "confidence": 0.95},
    "ldl cholesterol": {"code": "2089-1", "display": "LDL Cholesterol", "confidence": 0.94},
    "hdl cholesterol": {"code": "2085-9", "display": "HDL Cholesterol", "confidence": 0.94},
    "white blood cell count": {"code": "6690-2", "display": "Leukocytes [#/volume] in Blood", "confidence": 0.95},
    "platelet count": {"code": "777-3", "display": "Platelets [#/volume] in Blood", "confidence": 0.95},
    "hemoglobin": {"code": "718-7", "display": "Hemoglobin [Mass/volume] in Blood", "confidence": 0.96},
    "blood pressure systolic": {"code": "8480-6", "display": "Systolic blood pressure", "confidence": 0.95},
    "blood pressure diastolic": {"code": "8462-4", "display": "Diastolic blood pressure", "confidence": 0.95},
}


# ── Internal helpers ─────────────────────────────────────────────────────


def _normalise(term: str) -> str:
    """Lower-case and strip whitespace for lookup matching."""
    return term.strip().lower()


def _get_terminology_versions(trial_id: str | None) -> dict[str, str]:
    """Retrieve per-trial terminology versions from HealthLake ResearchStudy.

    Falls back to DEFAULT_TERMINOLOGY_VERSIONS when the trial has no config
    or the read fails.
    """
    if not trial_id:
        return dict(DEFAULT_TERMINOLOGY_VERSIONS)

    try:
        config = get_trial_config(trial_id)
        if config and config.get("terminology_versions"):
            versions = dict(DEFAULT_TERMINOLOGY_VERSIONS)
            versions.update(config["terminology_versions"])
            return versions
    except Exception:
        logger.warning(
            "Could not load terminology versions for trial %s — using defaults",
            trial_id,
        )

    return dict(DEFAULT_TERMINOLOGY_VERSIONS)


def _lookup_condition(term: str, target: str, version: str) -> dict | None:
    """Look up a condition term in SNOMED CT or ICD-10."""
    key = _normalise(term)
    if target == "snomed":
        entry = _CONDITION_SNOMED.get(key)
        if entry:
            return {
                "system": TERMINOLOGY_SYSTEMS["snomed"]["uri"],
                "system_display": TERMINOLOGY_SYSTEMS["snomed"]["display"],
                "version": version,
                "code": entry["code"],
                "display": entry["display"],
                "confidence": entry["confidence"],
            }
    elif target == "icd10":
        entry = _CONDITION_ICD10.get(key)
        if entry:
            return {
                "system": TERMINOLOGY_SYSTEMS["icd10"]["uri"],
                "system_display": TERMINOLOGY_SYSTEMS["icd10"]["display"],
                "version": version,
                "code": entry["code"],
                "display": entry["display"],
                "confidence": entry["confidence"],
            }
    return None


def _lookup_medication(term: str, target: str, version: str) -> dict | None:
    """Look up a medication term in RxNorm or WHO Drug Dictionary."""
    key = _normalise(term)
    if target == "rxnorm":
        entry = _MEDICATION_RXNORM.get(key)
        if entry:
            return {
                "system": TERMINOLOGY_SYSTEMS["rxnorm"]["uri"],
                "system_display": TERMINOLOGY_SYSTEMS["rxnorm"]["display"],
                "version": version,
                "code": entry["code"],
                "display": entry["display"],
                "confidence": entry["confidence"],
            }
    elif target == "who_drug":
        entry = _MEDICATION_WHO_DRUG.get(key)
        if entry:
            return {
                "system": TERMINOLOGY_SYSTEMS["who_drug"]["uri"],
                "system_display": TERMINOLOGY_SYSTEMS["who_drug"]["display"],
                "version": version,
                "code": entry["code"],
                "display": entry["display"],
                "confidence": entry["confidence"],
            }
    return None


def _lookup_adverse_event(term: str, version: str) -> dict | None:
    """Look up an adverse-event term in MedDRA."""
    key = _normalise(term)
    entry = _AE_MEDDRA.get(key)
    if entry:
        return {
            "system": TERMINOLOGY_SYSTEMS["meddra"]["uri"],
            "system_display": TERMINOLOGY_SYSTEMS["meddra"]["display"],
            "version": version,
            "code": entry["pt_code"],
            "display": entry["pt"],
            "soc_code": entry["soc_code"],
            "soc_display": entry["soc"],
            "confidence": entry["confidence"],
        }
    return None


def _lookup_lab(term: str, version: str) -> dict | None:
    """Look up a lab-test term in LOINC."""
    key = _normalise(term)
    entry = _LAB_LOINC.get(key)
    if entry:
        return {
            "system": TERMINOLOGY_SYSTEMS["loinc"]["uri"],
            "system_display": TERMINOLOGY_SYSTEMS["loinc"]["display"],
            "version": version,
            "code": entry["code"],
            "display": entry["display"],
            "confidence": entry["confidence"],
        }
    return None


def _fuzzy_search(term: str, lookup: dict[str, dict], threshold: float = 0.4) -> dict | None:
    """Simple substring-based fuzzy fallback when exact match fails.

    Returns the best match whose normalised key is contained in the term
    (or vice-versa), with a reduced confidence score.
    """
    key = _normalise(term)
    best: dict | None = None
    best_score = 0.0

    for candidate_key, entry in lookup.items():
        if candidate_key in key or key in candidate_key:
            # Penalise partial matches
            overlap = len(set(candidate_key) & set(key))
            score = overlap / max(len(candidate_key), len(key))
            if score > best_score and score >= threshold:
                best_score = score
                best = dict(entry)  # copy
                best["confidence"] = round(min(entry["confidence"], score), 2)

    return best


def _map_single_target(
    term: str,
    source_system: str,
    target: str,
    versions: dict[str, str],
) -> dict | None:
    """Attempt to map *term* to a single target terminology system."""
    version = versions.get(target, DEFAULT_TERMINOLOGY_VERSIONS.get(target, "unknown"))

    # Direct lookup by source hint
    if source_system in ("condition", "diagnosis"):
        if target in ("snomed", "icd10"):
            result = _lookup_condition(term, target, version)
            if result:
                return result
    elif source_system in ("medication", "drug"):
        if target in ("rxnorm", "who_drug"):
            result = _lookup_medication(term, target, version)
            if result:
                return result
    elif source_system in ("adverse_event", "ae"):
        if target == "meddra":
            result = _lookup_adverse_event(term, version)
            if result:
                return result
    elif source_system in ("lab", "laboratory", "observation"):
        if target == "loinc":
            result = _lookup_lab(term, version)
            if result:
                return result

    # Fallback: try all lookup tables for the requested target
    if target in ("snomed", "icd10"):
        result = _lookup_condition(term, target, version)
        if result:
            return result
    if target in ("rxnorm", "who_drug"):
        result = _lookup_medication(term, target, version)
        if result:
            return result
    if target == "meddra":
        result = _lookup_adverse_event(term, version)
        if result:
            return result
    if target == "loinc":
        result = _lookup_lab(term, version)
        if result:
            return result

    # Fuzzy fallback — pick the right lookup table for the target
    fuzzy_tables: dict[str, dict] = {
        "snomed": _CONDITION_SNOMED,
        "icd10": _CONDITION_ICD10,
        "rxnorm": _MEDICATION_RXNORM,
        "who_drug": _MEDICATION_WHO_DRUG,
        "loinc": _LAB_LOINC,
    }
    if target == "meddra":
        # Flatten MedDRA entries to match the shape expected by _fuzzy_search
        flat = {k: {"code": v["pt_code"], "display": v["pt"], "confidence": v["confidence"]} for k, v in _AE_MEDDRA.items()}
        fuzzy_tables["meddra"] = flat

    table = fuzzy_tables.get(target)
    if table:
        fuzzy = _fuzzy_search(term, table)
        if fuzzy:
            sys_info = TERMINOLOGY_SYSTEMS.get(target, {})
            return {
                "system": sys_info.get("uri", target),
                "system_display": sys_info.get("display", target),
                "version": version,
                "code": fuzzy["code"],
                "display": fuzzy["display"],
                "confidence": fuzzy["confidence"],
            }

    return None


# ── Public tool ──────────────────────────────────────────────────────────


@tool
def terminology_map(
    term: str,
    source_system: str,
    target_systems: list[str],
    trial_id: str | None = None,
) -> dict:
    """Map a clinical term across standard terminologies.

    Looks up *term* in each requested target terminology system and returns
    mappings with confidence scores.  Terms whose best confidence is below
    0.8 are flagged for manual review and logged via the Audit_Logger.

    Supported source_system hints: condition, diagnosis, medication, drug,
    adverse_event, ae, lab, laboratory, observation.

    Supported target systems: meddra, snomed, icd10, loinc, rxnorm, who_drug.

    Args:
        term: The clinical term to map (e.g. "type 2 diabetes", "metformin").
        source_system: Hint about the term's domain (condition, medication,
            adverse_event, lab).
        target_systems: List of target terminology keys to map into.
        trial_id: Optional trial identifier for per-trial terminology
            version selection from TrialProtocolConfig.

    Returns:
        Dict with:
            - term: the original term
            - source_system: echo of the source hint
            - mappings: list of mapping dicts (system, code, display,
              confidence, version, …)
            - best_confidence: highest confidence across all mappings
            - flagged_for_review: True when best_confidence < 0.8
            - terminology_versions: the version set used
    """
    versions = _get_terminology_versions(trial_id)

    mappings: list[dict] = []
    for target in target_systems:
        target_key = target.strip().lower()
        if target_key not in TERMINOLOGY_SYSTEMS:
            logger.warning("Unknown target terminology system: %s", target)
            continue
        result = _map_single_target(term, source_system.strip().lower(), target_key, versions)
        if result:
            mappings.append(result)

    best_confidence = max((m["confidence"] for m in mappings), default=0.0)
    flagged = best_confidence < CONFIDENCE_THRESHOLD

    # Log low-confidence / unmapped terms for manual review
    if flagged:
        try:
            audit_log_event.invoke(
                {
                    "session_id": f"terminology-review-{trial_id or 'global'}",
                    "event_type": "validation_performed",
                    "patient_id": "",
                    "trial_id": trial_id or "",
                    "agent_id": "terminology-service",
                    "user_identity": "terminology-service",
                    "event_data": {
                        "action": "terminology_mapping_flagged",
                        "term": term,
                        "source_system": source_system,
                        "target_systems": target_systems,
                        "best_confidence": best_confidence,
                        "mappings_found": len(mappings),
                    },
                }
            )
        except Exception:
            logger.exception("Failed to log flagged terminology mapping")

    return {
        "term": term,
        "source_system": source_system,
        "mappings": mappings,
        "best_confidence": best_confidence,
        "flagged_for_review": flagged,
        "terminology_versions": versions,
    }
