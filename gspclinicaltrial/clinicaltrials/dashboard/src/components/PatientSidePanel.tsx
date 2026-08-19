import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Loader2, User, Stethoscope, Activity, ChevronDown, ChevronRight } from "lucide-react";
import type { Patient } from "../types";
import type { TraceEntry } from "./AgentTrace";
import PatientInsightsPanel from "./PatientInsightsPanel";
import * as api from "../api";
import styles from "./PatientSidePanel.module.css";
import traceCss from "./AgentTrace.module.css";

// ── Re-export TraceEntry so App.tsx doesn't need to change its import ──────
export type { TraceEntry };

// ── FHIR helpers ────────────────────────────────────────────────────────────

interface FhirResource {
  code?: { text?: string; coding?: { display?: string; code?: string }[] };
  medicationCodeableConcept?: { text?: string; coding?: { display?: string }[] };
  valueQuantity?: { value?: number; unit?: string };
  valueString?: string;
  birthDate?: string;
  gender?: string;
  name?: { given?: string[]; family?: string }[];
  id?: string;
  [key: string]: unknown;
}

function extractLabel(resource: FhirResource, type: string): string {
  if (type === "medications") {
    const med = resource.medicationCodeableConcept;
    return med?.text || med?.coding?.[0]?.display || "Unknown medication";
  }
  if (type === "observations") {
    const code = resource.code;
    const display = code?.text || code?.coding?.[0]?.display || "Observation";
    const val = resource.valueQuantity;
    if (val?.value != null) return `${display}: ${val.value} ${val.unit || ""}`.trim();
    if (resource.valueString) return `${display}: ${resource.valueString}`;
    return display;
  }
  const code = resource.code;
  return code?.text || code?.coding?.[0]?.display || "—";
}

function getAge(birthDate: string): string {
  if (!birthDate) return "?";
  return String(Math.floor((Date.now() - new Date(birthDate).getTime()) / 31557600000));
}

// ── Tab types ────────────────────────────────────────────────────────────────

type Tab = "insights" | "clinical" | "trace";

interface Props {
  selectedPatient: Patient | null;
  traces: TraceEntry[];
  activeNode: string | null;
  completedNodes: Set<string>;
}

// ── FLOW_NODES (mirrors AgentTrace) ─────────────────────────────────────────

const FLOW_NODES = [
  "load_patient", "load_questionnaire", "ask_question",
  "validate_response", "check_discrepancy", "check_enable_when",
  "resolve_eligibility", "finalize_screening",
];

function formatNode(n: string) {
  return n.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Clinical tab — loads PatientDetail from /api/patient/{id} ────────────────

interface ClinicalSections {
  conditions: FhirResource[];
  medications: FhirResource[];
  observations: FhirResource[];
  allergies: FhirResource[];
  procedures: FhirResource[];
}

function ClinicalTab({ patient }: { patient: Patient }) {
  const [patientFhir, setPatientFhir] = useState<FhirResource | null>(null);
  const [sections, setSections] = useState<ClinicalSections | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["conditions", "medications"]));

  useEffect(() => {
    const controller = new AbortController();
    Promise.resolve().then(() => {
      setLoading(true);
      setSections(null);
    });
    api.fetchPatientDetail(patient.id).then((d) => {
      if (controller.signal.aborted) return;
      setPatientFhir(d.patient as FhirResource);
      setSections({
        conditions: (d.conditions ?? []) as FhirResource[],
        medications: (d.medications ?? []) as FhirResource[],
        observations: (d.observations ?? []) as FhirResource[],
        allergies: (d.allergies ?? []) as FhirResource[],
        procedures: (d.procedures ?? []) as FhirResource[],
      });
      setLoading(false);
    }).catch(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [patient.id]);

  const toggle = (key: string) => setExpanded(prev => {
    const next = new Set(prev);
    if (next.has(key)) { next.delete(key); } else { next.add(key); }
    return next;
  });

  if (loading) return (
    <div className={styles.stateRow}>
      <Loader2 size={14} className={styles.spin} />
      <span>Loading clinical data…</span>
    </div>
  );

  const birthDate = (patientFhir?.birthDate as string) || patient.birthDate || "";
  const gender = (patientFhir?.gender as string) || patient.gender || "";

  const clinicalSections: { key: keyof ClinicalSections; label: string }[] = [
    { key: "conditions", label: "Conditions" },
    { key: "medications", label: "Medications" },
    { key: "observations", label: "Lab Results" },
    { key: "allergies", label: "Allergies" },
    { key: "procedures", label: "Procedures" },
  ];

  return (
    <div className={styles.clinicalWrap}>
      {/* Demographics */}
      <div className={styles.demoGrid}>
        <div className={styles.demoCell}>
          <span className={styles.demoLabel}>Age</span>
          <span className={styles.demoVal}>{getAge(birthDate)}y</span>
        </div>
        <div className={styles.demoCell}>
          <span className={styles.demoLabel}>Gender</span>
          <span className={styles.demoVal} style={{ textTransform: "capitalize" }}>{gender || "—"}</span>
        </div>
        <div className={styles.demoCell}>
          <span className={styles.demoLabel}>DOB</span>
          <span className={styles.demoVal}>{birthDate || "—"}</span>
        </div>
        <div className={styles.demoCell}>
          <span className={styles.demoLabel}>FHIR ID</span>
          <span className={`${styles.demoVal} ${styles.mono}`}>{patient.id.slice(0, 10)}…</span>
        </div>
      </div>

      {/* Expandable sections */}
      {clinicalSections.map(({ key, label }) => {
        const items = sections?.[key] ?? [];
        const open = expanded.has(key);
        return (
          <div key={key} className={styles.clinSection}>
            <button
              className={styles.clinSectionHeader}
              onClick={() => toggle(key)}
              aria-expanded={open}
            >
              <span className={styles.clinSectionLabel}>{label}</span>
              <span className={styles.clinSectionCount}>{items.length}</span>
              {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            </button>
            {open && (
              <div className={styles.clinSectionBody}>
                {items.length === 0 ? (
                  <div className={styles.clinEmpty}>None on record</div>
                ) : (
                  items.map((item, i) => (
                    <div key={i} className={styles.clinItem}>
                      <span className={styles.clinDot} />
                      <span>{extractLabel(item, key)}</span>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Trace tab (lifted from AgentTrace) ───────────────────────────────────────

function TraceTab({ traces, activeNode, completedNodes }: {
  traces: TraceEntry[];
  activeNode: string | null;
  completedNodes: Set<string>;
}) {
  return (
    <div className={styles.traceWrap}>
      <div className={traceCss.traceList} style={{ flex: "0 0 auto", maxHeight: 200 }}>
        {traces.length === 0 ? (
          <div className={traceCss.empty}>No active screening session</div>
        ) : (
          traces.map((t, i) => (
            <div key={i} className={`${traceCss.traceNode} animate-in`}>
              <div className={`${traceCss.dot} ${traceCss[t.status]}`} />
              <div>
                <div className={traceCss.nodeName}>{formatNode(t.node)}</div>
                <div className={traceCss.nodeDesc}>{t.description}</div>
                <div className={traceCss.nodeTime}>{t.time}</div>
              </div>
            </div>
          ))
        )}
      </div>

      <div className={traceCss.flowDiagram}>
        <h4 className={traceCss.flowTitle}>Screening Flow</h4>
        <div className={traceCss.flowNodes}>
          {FLOW_NODES.map((node, i) => {
            let state = "pending";
            if (node === activeNode) state = "active";
            else if (completedNodes.has(node)) state = "done";
            return (
              <div key={node}>
                <div className={`${traceCss.flowNode} ${traceCss[`flow_${state}`]}`}>
                  {formatNode(node)}
                </div>
                {i < FLOW_NODES.length - 1 && (
                  <div className={traceCss.flowArrow}>↓</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

export default function PatientSidePanel({ selectedPatient, traces, activeNode, completedNodes }: Props) {
  const [tab, setTab] = useState<Tab>("insights");

  // Switch to trace tab when screening becomes active
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { if (traces.length > 0) setTab("trace"); }, [traces]);

  // Switch back to insights when a new patient is selected
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { if (selectedPatient) setTab("insights"); }, [selectedPatient?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const tabs: { id: Tab; label: string; icon: ReactNode }[] = [
    { id: "insights", label: "Insights", icon: <Activity size={13} /> },
    { id: "clinical", label: "Clinical", icon: <Stethoscope size={13} /> },
    { id: "trace",    label: "Trace",    icon: <User size={13} /> },
  ];

  return (
    <aside className={styles.panel} aria-label="Patient side panel">
      {/* Header */}
      {selectedPatient ? (
        <div className={styles.patientHeader}>
          <div className={styles.patientAvatar}>
            {selectedPatient.name.charAt(0).toUpperCase()}
          </div>
          <div className={styles.patientMeta}>
            <div className={styles.patientName}>{selectedPatient.name}</div>
            <div className={styles.patientSub} style={{ textTransform: "capitalize" }}>
              {selectedPatient.gender} · {getAge(selectedPatient.birthDate)}y
            </div>
          </div>
        </div>
      ) : (
        <div className={styles.noPatient}>
          <User size={20} className={styles.noPatientIcon} />
          <span>Select a patient</span>
        </div>
      )}

      {/* Tab bar */}
      <div className={styles.tabBar} role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`${styles.tab} ${tab === t.id ? styles.tabActive : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.icon}
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className={styles.tabContent}>
        {!selectedPatient ? (
          <div className={styles.stateRow}>
            <span>No patient selected</span>
          </div>
        ) : tab === "insights" ? (
          <PatientInsightsPanel patient={selectedPatient} compact />
        ) : tab === "clinical" ? (
          <ClinicalTab patient={selectedPatient} />
        ) : (
          <TraceTab
            traces={traces}
            activeNode={activeNode}
            completedNodes={completedNodes}
          />
        )}
      </div>
    </aside>
  );
}
