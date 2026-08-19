"""Safety Reporter — E2B R3 ICSR generation and transmission.

Generates Individual Case Safety Reports (ICSRs) in ICH E2B R3 XML format
from confirmed serious adverse events. Codes AEs to MedDRA PT/SOC via the
Terminology Service, includes suspect and concomitant medications coded to
WHO Drug Dictionary, validates against the E2B R3 schema, and transmits to
a safety database endpoint with retry logic.

Calculates regulatory deadlines:
  - 15 calendar days for serious AEs
  - 7 calendar days for fatal / life-threatening AEs

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

import logging
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from components.terminology import terminology_map
from config.settings import (
    AWS_REGION,
    EDC_RETRY_ATTEMPTS,
    EDC_RETRY_BACKOFF_SECONDS,
)
from tools.audit import audit_log_event

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

E2B_R3_NAMESPACE = "urn:hl7-org:v3"
E2B_R3_SCHEMA_VERSION = "2.1"

# Regulatory reporting deadlines (calendar days from PI awareness)
SERIOUS_AE_DEADLINE_DAYS = 15
FATAL_LIFE_THREATENING_DEADLINE_DAYS = 7

# Seriousness criteria that trigger the 7-day expedited deadline
EXPEDITED_SERIOUSNESS = {"death", "life-threatening"}

# Required ICSR sections for E2B R3 validation
_REQUIRED_ICSR_SECTIONS = {
    "safetyreportid",
    "primarysourcecountry",
    "occurcountry",
    "transmissiondateformat",
    "transmissiondate",
    "reporttype",
    "serious",
    "seriousnessdeath",
    "seriousnesslifethreatening",
    "seriousnesshospitalization",
    "seriousnessdisabling",
    "seriousnesscongenitalanomali",
    "seriousnessother",
    "receivedateformat",
    "receivedate",
    "receiptdateformat",
    "receiptdate",
    "reactionmeddraversionllt",
    "reactionmeddrapt",
    "drugcharacterization",
    "medicinalproduct",
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _calculate_regulatory_deadline(
    seriousness: list[str],
    pi_awareness_date: str,
) -> tuple[str, int]:
    """Return (deadline ISO date, deadline_days) based on seriousness criteria.

    Fatal or life-threatening → 7 calendar days.
    All other serious AEs     → 15 calendar days.
    """
    seriousness_lower = {s.lower().replace("_", "-") for s in seriousness}
    if seriousness_lower & EXPEDITED_SERIOUSNESS:
        days = FATAL_LIFE_THREATENING_DEADLINE_DAYS
    else:
        days = SERIOUS_AE_DEADLINE_DAYS

    awareness = datetime.fromisoformat(pi_awareness_date)
    deadline = awareness + timedelta(days=days)
    return deadline.date().isoformat(), days


def _code_ae_meddra(description: str, trial_id: str) -> dict:
    """Code an AE description to MedDRA PT and SOC via Terminology Service."""
    result = terminology_map.invoke({
        "term": description,
        "source_system": "adverse_event",
        "target_systems": ["meddra"],
        "trial_id": trial_id,
    })
    mappings = result.get("mappings", [])
    if mappings:
        m = mappings[0]
        return {
            "pt": m.get("display", ""),
            "pt_code": m.get("code", ""),
            "soc": m.get("soc_display", ""),
            "soc_code": m.get("soc_code", ""),
            "version": m.get("version", ""),
            "confidence": m.get("confidence", 0.0),
        }
    return {
        "pt": description,
        "pt_code": "",
        "soc": "",
        "soc_code": "",
        "version": "",
        "confidence": 0.0,
    }


def _code_medication_who(med_name: str, trial_id: str) -> dict:
    """Code a medication name to WHO Drug Dictionary via Terminology Service."""
    result = terminology_map.invoke({
        "term": med_name,
        "source_system": "medication",
        "target_systems": ["who_drug"],
        "trial_id": trial_id,
    })
    mappings = result.get("mappings", [])
    if mappings:
        m = mappings[0]
        return {
            "who_drug_code": m.get("code", ""),
            "display": m.get("display", ""),
            "version": m.get("version", ""),
        }
    return {"who_drug_code": "", "display": med_name, "version": ""}


def _seriousness_flag(seriousness: list[str], criterion: str) -> str:
    """Return '1' if *criterion* is present in the seriousness list, else '2'."""
    normalised = {s.lower().replace("_", "-").replace(" ", "-") for s in seriousness}
    return "1" if criterion in normalised else "2"


def _format_date_e2b(iso_date: str) -> str:
    """Convert an ISO-8601 date/datetime to E2B yyyyMMdd format."""
    dt = datetime.fromisoformat(iso_date)
    return dt.strftime("%Y%m%d")


# ── E2B R3 XML builder ───────────────────────────────────────────────────


def _build_icsr_xml(
    ae: dict,
    meddra_coding: dict,
    suspect_med_codings: list[dict],
    concomitant_med_codings: list[dict],
    safety_report_id: str,
    deadline_date: str,
    deadline_days: int,
) -> ET.Element:
    """Build an E2B R3 ICSR XML element tree from adverse event data.

    Returns the root <ichicsr> Element.
    """
    root = ET.Element("ichicsr", lang="en")

    # ── Header ────────────────────────────────────────────────────────
    header = ET.SubElement(root, "ichicsrmessageheader")
    ET.SubElement(header, "messagetype").text = "ichicsr"
    ET.SubElement(header, "messageformatversion").text = E2B_R3_SCHEMA_VERSION
    ET.SubElement(header, "messageformatrelease").text = "2.1"
    ET.SubElement(header, "messagenumb").text = safety_report_id
    ET.SubElement(header, "messagesenderidentifier").text = "clinical-trials-safety-reporter"
    ET.SubElement(header, "messagereceiveridentifier").text = "safety-database"
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    ET.SubElement(header, "messagedateformat").text = "204"
    ET.SubElement(header, "messagedate").text = now_str

    # ── Safety Report ─────────────────────────────────────────────────
    report = ET.SubElement(root, "safetyreport")
    ET.SubElement(report, "safetyreportversion").text = "1"
    ET.SubElement(report, "safetyreportid").text = safety_report_id
    ET.SubElement(report, "primarysourcecountry").text = "US"
    ET.SubElement(report, "occurcountry").text = "US"
    ET.SubElement(report, "transmissiondateformat").text = "102"
    ET.SubElement(report, "transmissiondate").text = datetime.now(timezone.utc).strftime("%Y%m%d")
    ET.SubElement(report, "reporttype").text = "1"  # spontaneous
    ET.SubElement(report, "serious").text = "1"  # always serious for ICSRs

    # Seriousness criteria flags
    seriousness = ae.get("seriousness", [])
    ET.SubElement(report, "seriousnessdeath").text = _seriousness_flag(seriousness, "death")
    ET.SubElement(report, "seriousnesslifethreatening").text = _seriousness_flag(seriousness, "life-threatening")
    ET.SubElement(report, "seriousnesshospitalization").text = _seriousness_flag(seriousness, "hospitalization")
    ET.SubElement(report, "seriousnessdisabling").text = _seriousness_flag(seriousness, "disability")
    ET.SubElement(report, "seriousnesscongenitalanomali").text = _seriousness_flag(seriousness, "congenital-anomaly")
    ET.SubElement(report, "seriousnessother").text = _seriousness_flag(seriousness, "other")

    # Receive / receipt dates
    report_date_e2b = _format_date_e2b(ae.get("report_date", datetime.now(timezone.utc).isoformat()))
    ET.SubElement(report, "receivedateformat").text = "102"
    ET.SubElement(report, "receivedate").text = report_date_e2b
    ET.SubElement(report, "receiptdateformat").text = "102"
    ET.SubElement(report, "receiptdate").text = report_date_e2b

    # Regulatory deadline extension
    deadline_ext = ET.SubElement(report, "regulatorydeadline")
    ET.SubElement(deadline_ext, "deadlinedate").text = deadline_date
    ET.SubElement(deadline_ext, "deadlinedays").text = str(deadline_days)

    # ── Primary source ────────────────────────────────────────────────
    primary_source = ET.SubElement(report, "primarysource")
    ET.SubElement(primary_source, "reportergivename").text = "Redacted"
    ET.SubElement(primary_source, "qualification").text = "1"  # physician

    # ── Patient ───────────────────────────────────────────────────────
    patient = ET.SubElement(report, "patient")
    ET.SubElement(patient, "patientinitial").text = ae.get("patient_id", "")[:3].upper()

    # ── Reaction (MedDRA coded) ───────────────────────────────────────
    reaction = ET.SubElement(patient, "reaction")
    ET.SubElement(reaction, "primarysourcereaction").text = ae.get("description", "")
    ET.SubElement(reaction, "reactionmeddraversionllt").text = meddra_coding.get("version", "")
    ET.SubElement(reaction, "reactionmeddrapt").text = meddra_coding.get("pt", "")
    ET.SubElement(reaction, "reactionmeddraptcode").text = meddra_coding.get("pt_code", "")
    ET.SubElement(reaction, "reactionmeddrasoc").text = meddra_coding.get("soc", "")
    ET.SubElement(reaction, "reactionmeddrasoccode").text = meddra_coding.get("soc_code", "")
    ET.SubElement(reaction, "reactionoutcome").text = _map_outcome(ae.get("outcome", "unknown"))

    if ae.get("onset_date"):
        ET.SubElement(reaction, "reactionstartdateformat").text = "102"
        ET.SubElement(reaction, "reactionstartdate").text = _format_date_e2b(ae["onset_date"])

    # ── Suspect drugs ─────────────────────────────────────────────────
    for i, med in enumerate(ae.get("suspect_medications", [])):
        drug = ET.SubElement(patient, "drug")
        ET.SubElement(drug, "drugcharacterization").text = "1"  # suspect
        ET.SubElement(drug, "medicinalproduct").text = med.get("name", "")
        coding = suspect_med_codings[i] if i < len(suspect_med_codings) else {}
        ET.SubElement(drug, "drugrecurreadministration").text = ""
        if coding.get("who_drug_code"):
            ET.SubElement(drug, "activesubstancename").text = coding.get("display", "")
            drugid = ET.SubElement(drug, "drugidentification")
            ET.SubElement(drugid, "drugcode").text = coding["who_drug_code"]
            ET.SubElement(drugid, "drugcodesystem").text = "WHO Drug Dictionary"

    # ── Concomitant drugs ─────────────────────────────────────────────
    for i, med in enumerate(ae.get("concomitant_medications", [])):
        drug = ET.SubElement(patient, "drug")
        ET.SubElement(drug, "drugcharacterization").text = "2"  # concomitant
        ET.SubElement(drug, "medicinalproduct").text = med.get("name", "")
        coding = concomitant_med_codings[i] if i < len(concomitant_med_codings) else {}
        if coding.get("who_drug_code"):
            ET.SubElement(drug, "activesubstancename").text = coding.get("display", "")
            drugid = ET.SubElement(drug, "drugidentification")
            ET.SubElement(drugid, "drugcode").text = coding["who_drug_code"]
            ET.SubElement(drugid, "drugcodesystem").text = "WHO Drug Dictionary"

    # ── Summary ───────────────────────────────────────────────────────
    summary = ET.SubElement(report, "summary")
    ET.SubElement(summary, "narrativeincludeclinical").text = (
        f"Serious adverse event: {ae.get('description', '')}. "
        f"Severity: {ae.get('severity', '')}. "
        f"Causality: {ae.get('causality', '')}. "
        f"Outcome: {ae.get('outcome', '')}."
    )

    return root


def _map_outcome(outcome: str) -> str:
    """Map outcome string to E2B R3 reaction outcome code."""
    mapping = {
        "recovered": "1",
        "recovering": "2",
        "not_recovered": "3",
        "not recovered": "3",
        "fatal": "5",
        "unknown": "0",
    }
    return mapping.get(outcome.lower(), "0")


# ── E2B R3 schema validation ─────────────────────────────────────────────


def _validate_icsr(root: ET.Element) -> list[str]:
    """Validate an ICSR XML tree against E2B R3 structural requirements.

    Returns a list of validation error strings (empty = valid).
    """
    errors: list[str] = []

    report = root.find("safetyreport")
    if report is None:
        errors.append("Missing <safetyreport> element")
        return errors

    # Check required top-level report elements
    for tag in [
        "safetyreportid",
        "primarysourcecountry",
        "occurcountry",
        "transmissiondateformat",
        "transmissiondate",
        "reporttype",
        "serious",
        "receivedateformat",
        "receivedate",
        "receiptdateformat",
        "receiptdate",
    ]:
        el = report.find(tag)
        if el is None or not (el.text or "").strip():
            errors.append(f"Missing or empty required element <{tag}>")

    # Seriousness flags
    for tag in [
        "seriousnessdeath",
        "seriousnesslifethreatening",
        "seriousnesshospitalization",
        "seriousnessdisabling",
        "seriousnesscongenitalanomali",
        "seriousnessother",
    ]:
        el = report.find(tag)
        if el is None or el.text not in ("1", "2"):
            errors.append(f"<{tag}> must be '1' or '2'")

    # Patient section
    patient = report.find("patient")
    if patient is None:
        errors.append("Missing <patient> element")
        return errors

    # At least one reaction
    reactions = patient.findall("reaction")
    if not reactions:
        errors.append("Missing <reaction> element in <patient>")
    else:
        for idx, rxn in enumerate(reactions):
            pt = rxn.find("reactionmeddrapt")
            if pt is None or not (pt.text or "").strip():
                errors.append(f"Reaction {idx}: missing <reactionmeddrapt>")
            ver = rxn.find("reactionmeddraversionllt")
            if ver is None or not (ver.text or "").strip():
                errors.append(f"Reaction {idx}: missing <reactionmeddraversionllt>")

    # At least one drug with characterization
    drugs = patient.findall("drug")
    if not drugs:
        errors.append("Missing <drug> element in <patient>")
    else:
        for idx, drg in enumerate(drugs):
            char = drg.find("drugcharacterization")
            if char is None or char.text not in ("1", "2", "3"):
                errors.append(f"Drug {idx}: invalid <drugcharacterization>")
            prod = drg.find("medicinalproduct")
            if prod is None or not (prod.text or "").strip():
                errors.append(f"Drug {idx}: missing <medicinalproduct>")

    return errors


def _icsr_to_string(root: ET.Element) -> str:
    """Serialize an ICSR XML element tree to a UTF-8 string."""
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ── Transmission helpers ─────────────────────────────────────────────────


def _transmit_to_endpoint(icsr_xml: str, endpoint_url: str) -> dict:
    """Transmit ICSR XML to a safety database endpoint.

    In production this would use requests/httpx to POST the XML.
    For this PoC we simulate the transmission.
    """
    # Simulate HTTP POST to safety database
    # In production: response = requests.post(endpoint_url, data=icsr_xml,
    #                headers={"Content-Type": "application/xml"})
    logger.info("Transmitting ICSR to %s (%d bytes)", endpoint_url, len(icsr_xml))
    return {"status": "acknowledged", "endpoint": endpoint_url}


def _transmit_with_retry(
    icsr_xml: str,
    endpoint_url: str,
    trial_id: str,
    session_id: str,
    icsr_id: str,
) -> dict:
    """Transmit ICSR with exponential backoff retry (5s, 15s, 45s).

    On final failure, queues for manual review and logs in Audit_Logger.
    """
    last_error = None
    for attempt in range(EDC_RETRY_ATTEMPTS):
        try:
            result = _transmit_to_endpoint(icsr_xml, endpoint_url)
            # Log successful transmission
            audit_log_event.invoke({
                "session_id": session_id,
                "event_type": "data_access",
                "trial_id": trial_id,
                "agent_id": "safety-reporter",
                "user_identity": "safety-reporter",
                "event_data": {
                    "action": "icsr_transmission",
                    "icsr_id": icsr_id,
                    "status": result.get("status", "sent"),
                    "endpoint": endpoint_url,
                    "attempt": attempt + 1,
                },
            })
            return {
                "icsr_id": icsr_id,
                "status": result.get("status", "sent"),
                "endpoint": endpoint_url,
            }
        except Exception as exc:
            last_error = exc
            if attempt < EDC_RETRY_ATTEMPTS - 1:
                backoff = EDC_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "ICSR transmission attempt %d failed, retrying in %ds: %s",
                    attempt + 1, backoff, exc,
                )
                time.sleep(backoff)

    # All retries exhausted
    logger.error(
        "ICSR transmission failed after %d attempts for %s",
        EDC_RETRY_ATTEMPTS, icsr_id,
    )
    audit_log_event.invoke({
        "session_id": session_id,
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "safety-reporter",
        "user_identity": "safety-reporter",
        "event_data": {
            "action": "icsr_transmission",
            "icsr_id": icsr_id,
            "status": "failed",
            "error": str(last_error),
            "retry_count": EDC_RETRY_ATTEMPTS,
            "queued_for_manual_review": True,
        },
    })
    return {
        "icsr_id": icsr_id,
        "status": "failed",
        "error": str(last_error),
        "queued_for_manual_review": True,
    }


# ── Public tools ─────────────────────────────────────────────────────────


@tool
def generate_e2b_icsr(adverse_event: dict, trial_id: str) -> dict:
    """Generate an E2B R3 Individual Case Safety Report from a serious AE.

    Codes the AE to MedDRA PT/SOC via the Terminology Service, includes
    suspect and concomitant medications coded to WHO Drug Dictionary,
    validates against the ICH E2B R3 schema, and calculates the regulatory
    reporting deadline.

    Args:
        adverse_event: Dict matching the AdverseEvent model with keys:
            ae_id, patient_id, trial_id, session_id, description,
            onset_date, severity, seriousness (list), causality, outcome,
            suspect_medications (list of {name, ...}),
            concomitant_medications (list of {name, ...}),
            reporter, report_date.
        trial_id: NCT number or internal trial identifier.

    Returns:
        Dict with icsr_id, icsr_xml (string), meddra_coding,
        suspect_med_codings, concomitant_med_codings, regulatory_deadline,
        deadline_days, validation_errors (list), and valid (bool).
    """
    icsr_id = f"ICSR-{uuid.uuid4().hex[:12].upper()}"

    # Code AE to MedDRA
    meddra_coding = _code_ae_meddra(
        adverse_event.get("description", ""), trial_id
    )

    # Code suspect medications to WHO Drug Dictionary
    suspect_med_codings = [
        _code_medication_who(med.get("name", ""), trial_id)
        for med in adverse_event.get("suspect_medications", [])
    ]

    # Code concomitant medications to WHO Drug Dictionary
    concomitant_med_codings = [
        _code_medication_who(med.get("name", ""), trial_id)
        for med in adverse_event.get("concomitant_medications", [])
    ]

    # Calculate regulatory deadline
    pi_awareness_date = adverse_event.get(
        "report_date", datetime.now(timezone.utc).isoformat()
    )
    deadline_date, deadline_days = _calculate_regulatory_deadline(
        adverse_event.get("seriousness", []), pi_awareness_date
    )

    # Build ICSR XML
    icsr_root = _build_icsr_xml(
        ae=adverse_event,
        meddra_coding=meddra_coding,
        suspect_med_codings=suspect_med_codings,
        concomitant_med_codings=concomitant_med_codings,
        safety_report_id=icsr_id,
        deadline_date=deadline_date,
        deadline_days=deadline_days,
    )

    # Validate against E2B R3 schema
    validation_errors = _validate_icsr(icsr_root)
    icsr_xml = _icsr_to_string(icsr_root)

    return {
        "icsr_id": icsr_id,
        "icsr_xml": icsr_xml,
        "meddra_coding": meddra_coding,
        "suspect_med_codings": suspect_med_codings,
        "concomitant_med_codings": concomitant_med_codings,
        "regulatory_deadline": deadline_date,
        "deadline_days": deadline_days,
        "validation_errors": validation_errors,
        "valid": len(validation_errors) == 0,
    }


@tool
def transmit_icsr(
    icsr_xml: str,
    endpoint_url: str,
    icsr_id: str = "",
    trial_id: str = "",
    session_id: str = "",
) -> dict:
    """Transmit an E2B R3 ICSR to a safety database endpoint.

    Retries with exponential backoff (5s, 15s, 45s). On final failure,
    queues for manual review and logs the failure in the Audit_Logger.

    Args:
        icsr_xml: The E2B R3 ICSR XML string to transmit.
        endpoint_url: Safety database endpoint URL.
        icsr_id: ICSR identifier for audit logging.
        trial_id: Trial identifier for audit logging.
        session_id: Session identifier for audit logging.

    Returns:
        Dict with icsr_id, status (sent/acknowledged/rejected/failed),
        endpoint, and optionally error and queued_for_manual_review.
    """
    result = _transmit_with_retry(
        icsr_xml=icsr_xml,
        endpoint_url=endpoint_url,
        trial_id=trial_id,
        session_id=session_id,
        icsr_id=icsr_id or f"ICSR-{uuid.uuid4().hex[:12].upper()}",
    )

    # Log transmission status in Audit_Logger
    audit_log_event.invoke({
        "session_id": session_id or "safety-reporter",
        "event_type": "data_access",
        "trial_id": trial_id,
        "agent_id": "safety-reporter",
        "user_identity": "safety-reporter",
        "event_data": {
            "action": "icsr_transmission_complete",
            "icsr_id": result.get("icsr_id", ""),
            "status": result.get("status", ""),
            "endpoint": endpoint_url,
        },
    })

    return result
