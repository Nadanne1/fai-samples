"""Protocol Engine for clinical trial protocol ingestion and Questionnaire generation.

Parses trial protocol inclusion/exclusion criteria into structured eligibility
rules, generates FHIR R4 Questionnaire resources with enableWhen conditional
logic for deep-dive modules, and integrates with ClinicalTrials.gov for
baseline protocol metadata.

Components:
    - parse_protocol_criteria: LLM-assisted parsing of I/E criteria
    - generate_fhir_questionnaire: FHIR Questionnaire R4 generation
    - clinicaltrials_gov_query: ClinicalTrials.gov API integration
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from components.terminology import terminology_map
from components.trial_config_store import get_trial_config, store_trial_config
from config.llm import get_protocol_llm
from config.settings import AWS_REGION
from tools.audit import audit_log_event
from tools.healthlake import healthlake_store_resource

logger = logging.getLogger(__name__)

# ClinicalTrials.gov v2 API base URL
_CTG_API_BASE = "https://clinicaltrials.gov/api/v2"
_CTG_TIMEOUT_SECONDS = 15

# Deep-dive module keywords for enableWhen activation
_DEEP_DIVE_MODULES = {
    "cardiovascular": [
        "heart", "cardiac", "cardiovascular", "coronary", "arrhythmia",
        "atrial fibrillation", "heart failure", "hypertension", "angina",
        "myocardial", "stroke", "vascular",
    ],
    "renal": [
        "kidney", "renal", "nephro", "dialysis", "creatinine", "egfr",
        "glomerular", "proteinuria", "ckd",
    ],
    "hepatic": [
        "liver", "hepatic", "hepato", "cirrhosis", "alt", "ast",
        "bilirubin", "jaundice", "hepatitis",
    ],
    "oncology": [
        "cancer", "tumor", "tumour", "malignant", "neoplasm", "oncology",
        "carcinoma", "lymphoma", "leukemia", "metastasis", "chemotherapy",
        "radiation",
    ],
}

# LLM for protocol parsing
_PARSE_SYSTEM_PROMPT = """You are a clinical trial protocol parser. Given trial protocol
inclusion/exclusion criteria text, extract each criterion into a structured JSON list.

For each criterion, produce:
- criterion_id: a unique identifier like "IE-001", "IE-002", etc. Use "IE-" prefix for
  inclusion and "EX-" prefix for exclusion criteria.
- description: the human-readable criterion text
- criterion_type: "inclusion" or "exclusion"
- data_type: one of "boolean", "integer", "decimal", "string", "date", "coding"
- fhir_path: the FHIR resource path relevant to this criterion (e.g. "Patient.birthDate",
  "Condition.code", "Observation.valueQuantity", "MedicationRequest.medicationCodeableConcept",
  "Procedure.code", "AllergyIntolerance.code")
- operator: one of "equals", "not_equals", "age_between", "greater_than", "less_than",
  "greater_or_equal", "less_or_equal", "exists", "not_exists", "contains", "in_list"
- value: the expected value, range (as [min, max]), or list of values

Return ONLY a JSON array of criterion objects. No markdown, no explanation."""



def _detect_deep_dive_module(description: str) -> str | None:
    """Detect which deep-dive module a criterion belongs to, if any."""
    import re as _re
    desc_lower = description.lower()
    for module, keywords in _DEEP_DIVE_MODULES.items():
        if any(_re.search(r'\b' + _re.escape(kw) + r'\b', desc_lower) for kw in keywords):
            return module
    return None


def _enrich_with_terminology(
    rules: list[dict], trial_id: str,
) -> list[dict]:
    """Map each eligibility rule to terminology codes via terminology_map.

    Adds terminology_codes to each rule based on its description and
    FHIR path context.
    """
    for rule in rules:
        description = rule.get("description", "")
        fhir_path = rule.get("fhir_path", "")

        # Determine source system hint from FHIR path
        if "Condition" in fhir_path:
            source = "condition"
            targets = ["snomed", "icd10"]
        elif "MedicationRequest" in fhir_path:
            source = "medication"
            targets = ["rxnorm", "who_drug"]
        elif "Observation" in fhir_path:
            source = "lab"
            targets = ["loinc"]
        elif "Procedure" in fhir_path:
            source = "condition"
            targets = ["snomed"]
        elif "AllergyIntolerance" in fhir_path:
            source = "condition"
            targets = ["snomed", "icd10"]
        else:
            # Patient demographics or generic — skip terminology mapping
            rule.setdefault("terminology_codes", [])
            continue

        try:
            result = terminology_map.invoke({
                "term": description,
                "source_system": source,
                "target_systems": targets,
                "trial_id": trial_id,
            })
            rule["terminology_codes"] = result.get("mappings", [])
        except Exception:
            logger.warning(
                "Terminology mapping failed for criterion %s",
                rule.get("criterion_id", "unknown"),
            )
            rule.setdefault("terminology_codes", [])

    return rules


@tool
def parse_protocol_criteria(protocol_document: str, trial_id: str) -> dict:
    """Parse trial protocol inclusion/exclusion criteria into structured eligibility rules.

    Uses ChatBedrockConverse for LLM-assisted parsing of free-text protocol
    criteria into structured eligibility rules with identifiers, descriptions,
    data types, and FHIR resource references. Each criterion is mapped to
    terminology codes via the Terminology_Service.

    Args:
        protocol_document: Free-text trial protocol containing inclusion and
            exclusion criteria sections.
        trial_id: Trial identifier (NCT number or internal ID) for terminology
            version selection and audit logging.

    Returns:
        Dict with:
            - trial_id: echo of the trial identifier
            - eligibility_rules: list of parsed EligibilityRule dicts
            - rule_count: total number of parsed rules
            - inclusion_count: number of inclusion criteria
            - exclusion_count: number of exclusion criteria
            - parse_errors: list of any parsing issues encountered
    """
    if not protocol_document or not protocol_document.strip():
        return {
            "trial_id": trial_id,
            "eligibility_rules": [],
            "rule_count": 0,
            "inclusion_count": 0,
            "exclusion_count": 0,
            "parse_errors": ["Empty protocol document provided"],
        }

    parse_errors: list[str] = []

    # Use LLM to parse criteria
    try:
        llm = get_protocol_llm()
        messages = [
            SystemMessage(content=_PARSE_SYSTEM_PROMPT),
            HumanMessage(content=protocol_document),
        ]
        response = llm.invoke(messages)
        raw_text = response.content

        # Extract JSON from response (handle potential markdown wrapping)
        import re as _re
        json_text = raw_text.strip()
        fence_match = _re.search(r"```(?:json)?\s*(.*?)\s*```", json_text, _re.DOTALL)
        if fence_match:
            json_text = fence_match.group(1)

        rules = json.loads(json_text)
        if not isinstance(rules, list):
            rules = [rules]

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM response as JSON: %s", exc)
        parse_errors.append(f"JSON parse error: {exc}")
        rules = []
    except Exception as exc:
        logger.error("LLM invocation failed: %s", exc)
        parse_errors.append(f"LLM error: {exc}")
        rules = []

    # Validate and normalise each rule
    valid_rules: list[dict] = []
    valid_types = {"boolean", "integer", "decimal", "string", "date", "coding"}
    valid_criterion_types = {"inclusion", "exclusion"}

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            parse_errors.append(f"Rule {i} is not a dict")
            continue

        # Ensure required fields
        criterion_id = rule.get("criterion_id", f"IE-{i + 1:03d}")
        criterion_type = rule.get("criterion_type", "inclusion").lower()
        if criterion_type not in valid_criterion_types:
            criterion_type = "inclusion"

        data_type = rule.get("data_type", "boolean").lower()
        if data_type not in valid_types:
            data_type = "boolean"

        valid_rules.append({
            "criterion_id": criterion_id,
            "description": rule.get("description", ""),
            "criterion_type": criterion_type,
            "data_type": data_type,
            "fhir_path": rule.get("fhir_path", ""),
            "operator": rule.get("operator", "equals"),
            "value": rule.get("value"),
            "terminology_codes": [],  # populated below
        })

    # Enrich with terminology codes
    if valid_rules:
        valid_rules = _enrich_with_terminology(valid_rules, trial_id)

    inclusion_count = sum(1 for r in valid_rules if r["criterion_type"] == "inclusion")
    exclusion_count = sum(1 for r in valid_rules if r["criterion_type"] == "exclusion")

    # Audit log the parsing event
    try:
        audit_log_event.invoke({
            "session_id": f"protocol-parse-{trial_id}",
            "event_type": "decision_made",
            "patient_id": "",
            "trial_id": trial_id,
            "agent_id": "protocol-engine",
            "user_identity": "protocol-engine",
            "event_data": {
                "action": "protocol_criteria_parsed",
                "rule_count": len(valid_rules),
                "inclusion_count": inclusion_count,
                "exclusion_count": exclusion_count,
                "parse_errors": parse_errors,
            },
        })
    except Exception:
        logger.exception("Failed to log protocol parsing event")

    return {
        "trial_id": trial_id,
        "eligibility_rules": valid_rules,
        "rule_count": len(valid_rules),
        "inclusion_count": inclusion_count,
        "exclusion_count": exclusion_count,
        "parse_errors": parse_errors,
    }



# ── FHIR Questionnaire Generation ────────────────────────────────────────


def _build_questionnaire_item(rule: dict) -> dict:
    """Build a single FHIR Questionnaire item from an eligibility rule.

    Maps the rule's data_type to a FHIR Questionnaire item type and attaches
    terminology codes and the eligibility-rule extension.
    """
    # Map eligibility rule data types to FHIR Questionnaire item types
    type_map = {
        "boolean": "boolean",
        "integer": "integer",
        "decimal": "decimal",
        "string": "string",
        "date": "date",
        "coding": "choice",
    }

    item: dict = {
        "linkId": rule["criterion_id"],
        "text": rule["description"],
        "type": type_map.get(rule.get("data_type", "boolean"), "boolean"),
        "required": True,
    }

    # Attach terminology codes
    codes = rule.get("terminology_codes", [])
    if codes:
        item["code"] = [
            {
                "system": c.get("system", ""),
                "code": c.get("code", ""),
                "display": c.get("display", ""),
            }
            for c in codes
        ]

    # Attach eligibility-rule extension with structured rule metadata
    item["extension"] = [
        {
            "url": "http://example.org/fhir/StructureDefinition/eligibility-rule",
            "valueString": json.dumps({
                "type": rule.get("criterion_type", "inclusion"),
                "fhir_path": rule.get("fhir_path", ""),
                "operator": rule.get("operator", "equals"),
                "value": rule.get("value"),
            }),
        }
    ]

    return item


def _build_deep_dive_group(module_name: str, module_rules: list[dict]) -> dict:
    """Build a deep-dive module group item with sub-items for each rule."""
    display_names = {
        "cardiovascular": "Cardiovascular Deep-Dive Module",
        "renal": "Renal Deep-Dive Module",
        "hepatic": "Hepatic Deep-Dive Module",
        "oncology": "Oncology Deep-Dive Module",
    }

    group: dict = {
        "linkId": f"DD-{module_name.upper()}",
        "text": display_names.get(module_name, f"{module_name.title()} Deep-Dive Module"),
        "type": "group",
        "required": False,
        "item": [_build_questionnaire_item(r) for r in module_rules],
    }

    return group


def _generate_human_readable_summary(
    questionnaire: dict, rules: list[dict],
) -> str:
    """Produce a human-readable summary of the generated Questionnaire for PI review."""
    lines: list[str] = []
    title = questionnaire.get("title", "Untitled Questionnaire")
    version = questionnaire.get("version", "unknown")
    lines.append(f"# {title} (v{version})")
    lines.append("")

    inclusion = [r for r in rules if r.get("criterion_type") == "inclusion"]
    exclusion = [r for r in rules if r.get("criterion_type") == "exclusion"]

    if inclusion:
        lines.append("## Inclusion Criteria")
        for r in inclusion:
            codes_str = ", ".join(
                f"{c.get('system_display', c.get('system', ''))}: {c.get('code', '')}"
                for c in r.get("terminology_codes", [])
            )
            code_note = f" [{codes_str}]" if codes_str else ""
            lines.append(f"- **{r['criterion_id']}**: {r['description']}{code_note}")
        lines.append("")

    if exclusion:
        lines.append("## Exclusion Criteria")
        for r in exclusion:
            codes_str = ", ".join(
                f"{c.get('system_display', c.get('system', ''))}: {c.get('code', '')}"
                for c in r.get("terminology_codes", [])
            )
            code_note = f" [{codes_str}]" if codes_str else ""
            lines.append(f"- **{r['criterion_id']}**: {r['description']}{code_note}")
        lines.append("")

    # Deep-dive modules
    deep_dive_items = [
        item for item in questionnaire.get("item", [])
        if item.get("linkId", "").startswith("DD-")
    ]
    if deep_dive_items:
        lines.append("## Deep-Dive Modules")
        for dd in deep_dive_items:
            sub_count = len(dd.get("item", []))
            lines.append(f"- **{dd['text']}** ({sub_count} questions)")
        lines.append("")

    total_items = len(questionnaire.get("item", []))
    lines.append(f"**Total Questionnaire items:** {total_items}")

    return "\n".join(lines)


@tool
def generate_fhir_questionnaire(eligibility_rules: list[dict], trial_id: str) -> dict:
    """Generate a FHIR Questionnaire R4 from eligibility rules with enableWhen logic.

    Creates a FHIR Questionnaire resource with items mapped to each eligibility
    criterion. Criteria that match deep-dive module keywords (cardiovascular,
    renal, hepatic, oncology) are grouped into enableWhen-gated sub-groups.
    The Questionnaire is stored in HealthLake with a trial protocol identifier
    and version reference.

    Args:
        eligibility_rules: List of parsed eligibility rule dicts (from
            parse_protocol_criteria).
        trial_id: Trial identifier (NCT number or internal ID).

    Returns:
        Dict with:
            - questionnaire_id: the FHIR resource ID
            - questionnaire: the full FHIR Questionnaire resource
            - summary: human-readable summary for PI review
            - deep_dive_modules: list of activated deep-dive module names
            - store_status: HealthLake persistence status
    """
    if not eligibility_rules:
        return {
            "questionnaire_id": None,
            "questionnaire": None,
            "summary": "No eligibility rules provided.",
            "deep_dive_modules": [],
            "store_status": "skipped",
        }

    questionnaire_id = f"trial-{trial_id}-screening-v1"

    # Separate rules into main items and deep-dive module items
    main_rules: list[dict] = []
    deep_dive_buckets: dict[str, list[dict]] = {}

    for rule in eligibility_rules:
        module = _detect_deep_dive_module(rule.get("description", ""))
        if module:
            deep_dive_buckets.setdefault(module, []).append(rule)
        else:
            main_rules.append(rule)

    # Build top-level Questionnaire items
    items: list[dict] = []
    for rule in main_rules:
        items.append(_build_questionnaire_item(rule))

    # Build deep-dive module groups with enableWhen logic
    # Each deep-dive group is enabled when a related main criterion is answered
    for module_name, module_rules in deep_dive_buckets.items():
        group = _build_deep_dive_group(module_name, module_rules)

        # Find a triggering criterion — the first main inclusion criterion
        # that could logically gate this module. If none found, enable
        # when the first main item is answered true.
        trigger_link_id = None
        for main_item in items:
            if main_item.get("type") == "boolean":
                trigger_link_id = main_item["linkId"]
                break

        if trigger_link_id:
            group["enableWhen"] = [
                {
                    "question": trigger_link_id,
                    "operator": "=",
                    "answerBoolean": True,
                }
            ]

        items.append(group)

    # Assemble the FHIR Questionnaire resource
    questionnaire: dict = {
        "resourceType": "Questionnaire",
        "id": questionnaire_id,
        "status": "active",
        "title": f"{trial_id} Eligibility Screening Questionnaire",
        "identifier": [
            {"system": "https://clinicaltrials.gov", "value": trial_id},
        ],
        "version": "1.0",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "item": items,
    }

    # Generate human-readable summary for PI review
    summary = _generate_human_readable_summary(questionnaire, eligibility_rules)

    # Store in HealthLake
    store_status = "not_attempted"
    try:
        store_result = healthlake_store_resource.invoke({"resource": questionnaire})
        store_status = store_result.get("status", "unknown")
        if "resource_id" in store_result:
            questionnaire["id"] = store_result["resource_id"]
            questionnaire_id = store_result["resource_id"]
    except Exception:
        logger.exception("Failed to store Questionnaire in HealthLake")
        store_status = "failed"

    # Audit log
    try:
        audit_log_event.invoke({
            "session_id": f"protocol-questionnaire-{trial_id}",
            "event_type": "decision_made",
            "patient_id": "",
            "trial_id": trial_id,
            "agent_id": "protocol-engine",
            "user_identity": "protocol-engine",
            "event_data": {
                "action": "questionnaire_generated",
                "questionnaire_id": questionnaire_id,
                "item_count": len(items),
                "deep_dive_modules": list(deep_dive_buckets.keys()),
                "store_status": store_status,
            },
        })
    except Exception:
        logger.exception("Failed to log questionnaire generation event")

    return {
        "questionnaire_id": questionnaire_id,
        "questionnaire": questionnaire,
        "summary": summary,
        "deep_dive_modules": list(deep_dive_buckets.keys()),
        "store_status": store_status,
    }



# ── ClinicalTrials.gov Integration ───────────────────────────────────────


def _parse_ctg_eligibility(eligibility_module: dict) -> str:
    """Extract eligibility criteria text from ClinicalTrials.gov eligibility module."""
    criteria_text = eligibility_module.get("eligibilityCriteria", "")
    return criteria_text


@tool
def clinicaltrials_gov_query(nct_number: str) -> dict:
    """Query ClinicalTrials.gov API for trial metadata and eligibility criteria.

    Retrieves protocol metadata (title, phase, status, conditions, interventions,
    eligibility criteria) for the given NCT number. Implements graceful
    degradation: on API unavailability, returns locally stored metadata if
    available and logs the failure.

    The retrieved metadata is stored in the TrialProtocolConfig DynamoDB table.

    Args:
        nct_number: The ClinicalTrials.gov NCT identifier (e.g. "NCT00000001").

    Returns:
        Dict with:
            - nct_number: echo of the NCT identifier
            - title: trial title
            - phase: trial phase
            - overall_status: trial status
            - conditions: list of studied conditions
            - interventions: list of interventions
            - eligibility_criteria: raw eligibility criteria text
            - source: "clinicaltrials.gov" or "local_cache"
            - config_version: version string from TrialProtocolConfig
            - error: error message if API call failed (absent on success)
    """
    trial_id = nct_number.strip().upper()

    # Attempt ClinicalTrials.gov v2 API
    try:
        url = f"{_CTG_API_BASE}/studies/{trial_id}"
        params = {
            "format": "json",
            "fields": (
                "NCTId,BriefTitle,OfficialTitle,OverallStatus,"
                "Phase,Condition,InterventionName,EligibilityModule"
            ),
        }
        resp = requests.get(url, params=params, timeout=_CTG_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()

        # v2 API nests under protocolSection
        protocol = data.get("protocolSection", data)
        id_module = protocol.get("identificationModule", {})
        status_module = protocol.get("statusModule", {})
        design_module = protocol.get("designModule", {})
        conditions_module = protocol.get("conditionsModule", {})
        arms_module = protocol.get("armsInterventionsModule", {})
        eligibility_module = protocol.get("eligibilityModule", {})

        title = id_module.get("officialTitle") or id_module.get("briefTitle", "")
        phases = design_module.get("phases", [])
        phase = phases[0] if phases else ""
        overall_status = status_module.get("overallStatus", "")
        conditions = conditions_module.get("conditions", [])
        interventions = [
            i.get("name", "")
            for i in arms_module.get("interventions", [])
        ]
        eligibility_criteria = _parse_ctg_eligibility(eligibility_module)

        nct_metadata = {
            "nct_number": trial_id,
            "title": title,
            "phase": phase,
            "overall_status": overall_status,
            "conditions": conditions,
            "interventions": interventions,
            "eligibility_criteria": eligibility_criteria,
        }

        # Store in TrialProtocolConfig
        config_version = store_trial_config(trial_id=trial_id, nct_metadata=nct_metadata)

        return {
            "nct_number": trial_id,
            "title": title,
            "phase": phase,
            "overall_status": overall_status,
            "conditions": conditions,
            "interventions": interventions,
            "eligibility_criteria": eligibility_criteria,
            "source": "clinicaltrials.gov",
            "config_version": config_version,
        }

    except Exception as api_error:
        logger.warning(
            "ClinicalTrials.gov API failed for %s: %s — falling back to local cache",
            trial_id,
            api_error,
        )

        # Log the failure
        try:
            audit_log_event.invoke({
                "session_id": f"ctg-query-{trial_id}",
                "event_type": "data_access",
                "patient_id": "",
                "trial_id": trial_id,
                "agent_id": "protocol-engine",
                "user_identity": "protocol-engine",
                "event_data": {
                    "action": "clinicaltrials_gov_query_failed",
                    "nct_number": trial_id,
                    "error": str(api_error),
                },
            })
        except Exception:
            logger.exception("Failed to log CTG query failure")

        # Graceful degradation: try local HealthLake ResearchStudy cache
        try:
            cached = get_trial_config(trial_id)
            if cached:
                meta = cached.get("nct_metadata", {})
                return {
                    "nct_number": trial_id,
                    "title": meta.get("title", ""),
                    "phase": meta.get("phase", ""),
                    "overall_status": meta.get("overall_status", ""),
                    "conditions": meta.get("conditions", []),
                    "interventions": meta.get("interventions", []),
                    "eligibility_criteria": meta.get("eligibility_criteria", ""),
                    "source": "local_cache",
                    "config_version": cached.get("version", ""),
                }
        except Exception:
            logger.exception("Failed to read local cache for %s", trial_id)

        return {
            "nct_number": trial_id,
            "title": "",
            "phase": "",
            "overall_status": "",
            "conditions": [],
            "interventions": [],
            "eligibility_criteria": "",
            "source": "unavailable",
            "config_version": "",
            "error": str(api_error),
        }
