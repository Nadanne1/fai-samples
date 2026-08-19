import { useEffect, useRef, useState } from "react";
import { Loader2, ChevronDown, ChevronRight } from "lucide-react";
import Modal, { ModalTabs } from "./Modal";
import Badge from "./Badge";
import * as api from "../api";
import styles from "./PatientDetailModal.module.css";

interface PatientDetailModalProps {
  patientId: string | null;
  onClose: () => void;
  onOpenPatientFromHistory?: (patientId: string) => void;
}

interface FhirResource {
  code?: { text?: string; coding?: { display?: string }[] };
  medicationCodeableConcept?: { text?: string; coding?: { display?: string }[] };
  valueQuantity?: { value?: number; unit?: string };
  valueString?: string;
  reaction?: { substance?: { text?: string } }[];
  [key: string]: unknown;
}

function extractDisplay(resource: FhirResource, type: string): string {
  if (type === "medications") {
    const med = resource.medicationCodeableConcept;
    if (med?.text) return med.text;
    if (med?.coding?.[0]?.display) return med.coding[0].display;
    return "Unknown medication";
  }
  if (type === "observations") {
    const code = resource.code;
    const display = code?.text || code?.coding?.[0]?.display || "Observation";
    const val = resource.valueQuantity;
    if (val?.value != null) return `${display}: ${val.value} ${val.unit || ""}`;
    if (resource.valueString) return `${display}: ${resource.valueString}`;
    return display;
  }
  if (type === "allergies") {
    const code = resource.code;
    return code?.text || code?.coding?.[0]?.display || "Unknown allergy";
  }
  const code = resource.code;
  return code?.text || code?.coding?.[0]?.display || "Unknown";
}

function getAge(birthDate: string): string {
  if (!birthDate) return "Unknown";
  return String(Math.floor((Date.now() - new Date(birthDate).getTime()) / 31557600000));
}

interface TrialHistoryEntry {
  id: string;
  trial_id: string;
  session_id: string;
  authored: string;
  determination: string;
  criteria_results: { linkId: string; text: string; result: string }[];
  item_count: number;
}

export default function PatientDetailModal({
  patientId,
  onClose,
}: PatientDetailModalProps) {
  const [tab, setTab] = useState("Patient Info");
  const [loading, setLoading] = useState(true);
  const [patientData, setPatientData] = useState<Record<string, unknown> | null>(null);
  const [sections, setSections] = useState<Record<string, FhirResource[]>>({});
  const [history, setHistory] = useState<TrialHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [expandedHistory, setExpandedHistory] = useState<Set<string>>(new Set());

  const historyFetchedRef = useRef(false);

  // Reset historyFetchedRef when patientId changes
  useEffect(() => {
    historyFetchedRef.current = false;
  }, [patientId]);

  // Load patient detail with AbortController
  useEffect(() => {
    if (!patientId) return;
    const controller = new AbortController();
    setExpandedHistory(new Set()); // eslint-disable-line react-hooks/set-state-in-effect

    api.fetchPatientDetail(patientId).then((data) => {
      if (controller.signal.aborted) return;
      setPatientData(data.patient);
      setSections({
        conditions: (data.conditions ?? []) as FhirResource[],
        medications: (data.medications ?? []) as FhirResource[],
        observations: (data.observations ?? []) as FhirResource[],
        allergies: (data.allergies ?? []) as FhirResource[],
        procedures: (data.procedures ?? []) as FhirResource[],
      });
      setLoading(false);
    }).catch(() => {
      if (!controller.signal.aborted) setLoading(false);
    });

    return () => controller.abort();
  }, [patientId]);

  // Load trial history when tab switches — guarded by ref to prevent re-run loop
  useEffect(() => {
    if (tab === "Trial History" && patientId && !historyFetchedRef.current) {
      historyFetchedRef.current = true;
      setHistoryLoading(true);
      api.fetchPatientTrialHistory(patientId).then((data) => {
        setHistory((data.history ?? []) as unknown as TrialHistoryEntry[]);
      }).catch(() => {
        setHistory([]);
      }).finally(() => {
        setHistoryLoading(false);
      });
    }
  }, [tab, patientId]);

  const toggleHistoryExpand = (id: string) => {
    setExpandedHistory((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Extract patient name from FHIR resource
  const patient = patientData as Record<string, unknown> | null;
  const nameObj = (patient?.name as { given?: string[]; family?: string }[] | undefined)?.[0];
  const patientName = nameObj
    ? `${(nameObj.given || []).join(" ")} ${nameObj.family || ""}`.trim()
    : patientId?.slice(0, 12) || "Patient";

  return (
    <Modal open={!!patientId} onClose={onClose} title={patientName} width={700}>
      <ModalTabs
        tabs={["Patient Info", "Trial History"]}
        activeTab={tab}
        onTabChange={setTab}
      />

      <div className={styles.body}>
        {tab === "Patient Info" && (
          loading ? (
            <div className={styles.loading}>
              <Loader2 size={20} className={styles.spin} />
              Loading patient data…
            </div>
          ) : (
            <>
              {/* Demographics grid */}
              <div className={styles.infoGrid}>
                <InfoCard label="Name" value={patientName} />
                <InfoCard label="Gender" value={String(patient?.gender || "Unknown")} />
                <InfoCard label="Date of Birth" value={String(patient?.birthDate || "Unknown")} />
                <InfoCard label="Age" value={getAge(String(patient?.birthDate || ""))} />
                <InfoCard
                  label="FHIR ID"
                  value={String(patient?.id || patientId || "")}
                  mono
                />
              </div>

              {/* Clinical sections */}
              <Section
                title="Conditions"
                items={sections.conditions || []}
                type="conditions"
                emptyText="No conditions on record"
              />
              <Section
                title="Medications"
                items={sections.medications || []}
                type="medications"
                emptyText="No medications on record"
              />
              <Section
                title="Observations"
                items={sections.observations || []}
                type="observations"
                emptyText="No observations on record"
              />
              <Section
                title="Allergies"
                items={sections.allergies || []}
                type="allergies"
                emptyText="No allergies on record"
              />
              <Section
                title="Procedures"
                items={sections.procedures || []}
                type="procedures"
                emptyText="No procedures on record"
              />
            </>
          )
        )}

        {tab === "Trial History" && (
          historyLoading ? (
            <div className={styles.loading}>
              <Loader2 size={20} className={styles.spin} />
              Loading trial history…
            </div>
          ) : history.length === 0 ? (
            <div className={styles.emptyHistory}>No screening history for this patient</div>
          ) : (
            <div className={styles.historyList}>
              {history.map((entry) => {
                const isExpanded = expandedHistory.has(entry.id);
                const variant =
                  entry.determination === "eligible"
                    ? "eligible"
                    : entry.determination === "ineligible"
                    ? "ineligible"
                    : entry.determination === "borderline"
                    ? "borderline"
                    : "neutral";

                return (
                  <div key={entry.id} className={styles.historyItem}>
                    <div
                      className={styles.historyHeader}
                      onClick={() => toggleHistoryExpand(entry.id)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => e.key === "Enter" && toggleHistoryExpand(entry.id)}
                      aria-expanded={isExpanded}
                    >
                      <div className={styles.historyChevron}>
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </div>
                      <div className={styles.historyInfo}>
                        <span className={styles.historyTrial}>{entry.trial_id || "Unknown Trial"}</span>
                        <span className={styles.historyDate}>
                          {entry.authored
                            ? new Date(entry.authored).toLocaleDateString()
                            : "—"}
                        </span>
                      </div>
                      <Badge variant={variant}>
                        {entry.determination || "unknown"}
                      </Badge>
                      <div className={styles.historyMeta}>
                        <span className={styles.mono}>{entry.session_id || entry.id.slice(0, 10)}</span>
                        <span>{entry.criteria_results?.length || entry.item_count || 0} criteria</span>
                      </div>
                    </div>

                    {isExpanded && entry.criteria_results?.length > 0 && (
                      <div className={styles.criteriaBreakdown}>
                        {entry.criteria_results.map((cr, i) => (
                          <div key={i} className={styles.criterionRow}>
                            <span
                              className={`${styles.criterionDot} ${
                                cr.result === "pass" ? styles.dotPass : styles.dotFail
                              }`}
                            />
                            <span className={styles.criterionText}>{cr.text}</span>
                            <span
                              className={
                                cr.result === "pass"
                                  ? styles.criterionPass
                                  : styles.criterionFail
                              }
                            >
                              {cr.result}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )
        )}
      </div>
    </Modal>
  );
}

/* Sub-components */

function InfoCard({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className={styles.infoCard}>
      <div className={styles.infoLabel}>{label}</div>
      <div className={`${styles.infoValue} ${mono ? styles.mono : ""}`}>{value}</div>
    </div>
  );
}

function Section({
  title,
  items,
  type,
  emptyText,
}: {
  title: string;
  items: FhirResource[];
  type: string;
  emptyText: string;
}) {
  return (
    <div className={styles.section}>
      <h4 className={styles.sectionTitle}>{title}</h4>
      {items.length === 0 ? (
        <div className={styles.sectionEmpty}>{emptyText}</div>
      ) : (
        items.map((item, i) => (
          <div key={i} className={styles.listItem}>
            {extractDisplay(item, type)}
          </div>
        ))
      )}
    </div>
  );
}
