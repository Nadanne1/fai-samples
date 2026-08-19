import { useEffect, useRef, useState } from "react";
import { Loader2, ChevronDown, ChevronRight } from "lucide-react";
import Modal, { ModalTabs } from "./Modal";
import Badge from "./Badge";
import type { TrialDetail } from "../types";
import type { HealthLakeMapping } from "../api";
import * as api from "../api";
import styles from "./TrialDetailModal.module.css";

interface TrialDetailModalProps {
  trialId: string | null;
  onClose: () => void;
  onOpenPatient: (patientId: string) => void;
}

const TABS = ["Overview", "Eligibility Criteria", "Screenings", "HealthLake Mapping", "Failure Analysis"];

export default function TrialDetailModal({
  trialId,
  onClose,
  onOpenPatient,
}: TrialDetailModalProps) {
  const [tab, setTab] = useState(TABS[0]);
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<TrialDetail | null>(null);

  useEffect(() => {
    if (!trialId) return;
    const controller = new AbortController();
    Promise.resolve().then(() => {
      setLoading(true);
      setTab(TABS[0]);
    });
    api.fetchTrialDetail(trialId, controller.signal)
      .then((d) => { if (!controller.signal.aborted) setData(d); })
      .catch(() => { /* show empty state */ })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [trialId]);

  const title = data?.title || trialId || "Trial";

  return (
    <Modal open={!!trialId} onClose={onClose} title={title} width={800}>
      <ModalTabs tabs={TABS} activeTab={tab} onTabChange={setTab} />

      <div className={styles.body}>
        {loading ? (
          <div className={styles.loading}>
            <Loader2 size={20} className={styles.spin} />
            Loading trial details…
          </div>
        ) : data ? (
          <>
            {tab === "Overview" && <OverviewTab data={data} />}
            {tab === "Eligibility Criteria" && <CriteriaTab data={data} />}
            {tab === "Screenings" && (
              <ScreeningsTab data={data} onOpenPatient={onOpenPatient} />
            )}
            {tab === "HealthLake Mapping" && <MappingTab trialId={trialId!} />}
            {tab === "Failure Analysis" && <FailureTab data={data} />}
          </>
        ) : null}
      </div>
    </Modal>
  );
}

/* ── Overview Tab ── */
function OverviewTab({ data }: { data: TrialDetail }) {
  const total = data.total_screenings;
  const eligible = data.by_determination.eligible || 0;
  const ineligible = data.by_determination.ineligible || 0;
  const rate = total > 0 ? Math.round((eligible / total) * 100) : 0;

  return (
    <>
      {/* Metadata cards */}
      <div className={styles.metaGrid}>
        <MetaCard label="Title" value={data.title} />
        <MetaCard label="Phase" value={data.phase} />
        <MetaCard label="Status" value={data.status} />
        <MetaCard label="Conditions" value={data.conditions.join(", ") || "—"} />
        <MetaCard label="Interventions" value={data.interventions.join(", ") || "—"} />
      </div>

      {/* Screening stats */}
      <div className={styles.statsGrid}>
        <div className={styles.statCard}>
          <div className={styles.statLabel}>Total Screenings</div>
          <div className={styles.statValue}>{total}</div>
        </div>
        <div className={`${styles.statCard} ${styles.statGreen}`}>
          <div className={styles.statLabel}>Eligible</div>
          <div className={styles.statValue} style={{ color: "var(--green)" }}>{eligible}</div>
        </div>
        <div className={`${styles.statCard} ${styles.statRed}`}>
          <div className={styles.statLabel}>Ineligible</div>
          <div className={styles.statValue} style={{ color: "var(--red)" }}>{ineligible}</div>
        </div>
        <div className={styles.statCard}>
          <div className={styles.statLabel}>Eligibility Rate</div>
          <div className={styles.statValue} style={{ color: "var(--accent-text)" }}>{rate}%</div>
        </div>
      </div>

      {/* Blinding */}
      {data.blinding && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Blinding Configuration</h4>
          <div className={styles.metaCard}>
            {(data.blinding as { is_blinded?: boolean }).is_blinded ? "Blinded" : "Open-label"}
            {(data.blinding as { unblinding_authority?: string[] }).unblinding_authority?.length
              ? ` · Unblinding: ${(data.blinding as { unblinding_authority: string[] }).unblinding_authority.join(", ")}`
              : ""}
          </div>
        </div>
      )}

      {/* Terminology versions */}
      {Object.keys(data.terminology_versions).length > 0 && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Terminology Versions</h4>
          <div className={styles.chipRow}>
            {Object.entries(data.terminology_versions).map(([key, ver]) => (
              <span key={key} className={styles.chip}>
                {key.toUpperCase()}: {ver}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Retention */}
      <div className={styles.section}>
        <h4 className={styles.sectionTitle}>Data Retention</h4>
        <div className={styles.metaCard}>{data.retention_years} years</div>
      </div>
    </>
  );
}

/* ── Eligibility Criteria Tab ── */
function CriteriaTab({ data }: { data: TrialDetail }) {
  if (!data.eligibility_rules?.length) {
    return <div className={styles.empty}>No eligibility criteria defined</div>;
  }

  return (
    <div className={styles.criteriaList}>
      {data.eligibility_rules.map((rule) => {
        const isInclusion = rule.type === "inclusion";
        const borderColor = isInclusion ? "var(--green)" : "var(--red)";

        return (
          <div
            key={rule.id}
            className={styles.criterionCard}
            style={{ borderLeftColor: borderColor }}
          >
            <div className={styles.criterionTop}>
              <span className={styles.criterionDesc}>{rule.description}</span>
              <Badge variant={isInclusion ? "eligible" : "ineligible"}>
                {rule.type}
              </Badge>
            </div>
            <div className={styles.criterionMeta}>
              <span>{rule.id}</span>
              <span>·</span>
              <span>{rule.data_type || "boolean"}</span>
              {rule.fhir_path && (
                <>
                  <span>·</span>
                  <span className={styles.mono}>{rule.fhir_path}</span>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Screenings Tab ── */
function ScreeningsTab({
  data,
  onOpenPatient,
}: {
  data: TrialDetail;
  onOpenPatient: (id: string) => void;
}) {
  if (!data.screenings?.length) {
    return <div className={styles.empty}>No screenings yet</div>;
  }

  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Date</th>
          <th>Patient</th>
          <th>Result</th>
          <th>Criteria</th>
        </tr>
      </thead>
      <tbody>
        {data.screenings.map((s) => {
          const variant =
            s.determination === "eligible"
              ? "eligible"
              : s.determination === "ineligible"
              ? "ineligible"
              : s.determination === "borderline"
              ? "borderline"
              : "neutral";

          return (
            <tr key={s.id}>
              <td>
                {s.authored
                  ? `${new Date(s.authored).toLocaleDateString()} ${new Date(s.authored).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                  : "—"}
              </td>
              <td>
                <button
                  className={styles.patientLink}
                  onClick={() => onOpenPatient(s.patient_id)}
                >
                  {s.patient_id.slice(0, 12)}…
                </button>
              </td>
              <td>
                <Badge variant={variant}>{s.determination}</Badge>
              </td>
              <td>{s.item_count}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/* ── Failure Analysis Tab ── */
function FailureTab({ data }: { data: TrialDetail }) {
  const failures = data.top_failure_criteria || [];

  if (failures.length === 0) {
    return <div className={styles.empty}>No failure data yet</div>;
  }

  const maxCount = failures[0][1];

  return (
    <>
      <div className={styles.failureIntro}>
        Most common reasons patients fail eligibility criteria
      </div>
      <div className={styles.failureList}>
        {failures.map(([criterion, count], i) => {
          const pct = Math.round((count / maxCount) * 100);
          return (
            <div key={i} className={styles.failureRow}>
              <div className={styles.failureTop}>
                <span className={styles.failureCriterion}>{criterion}</span>
                <span className={styles.failureCount}>
                  {count} failure{count > 1 ? "s" : ""}
                </span>
              </div>
              <div className={styles.failureTrack}>
                <div
                  className={styles.failureFill}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

/* ── HealthLake Mapping Tab ── */
function MappingTab({ trialId }: { trialId: string }) {
  const [data, setData] = useState<HealthLakeMapping | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["Questionnaire", "TrialProtocolConfig"]));
  const mappingControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    mappingControllerRef.current?.abort();
    const controller = new AbortController();
    mappingControllerRef.current = controller;
    Promise.resolve().then(() => setLoading(true));
    api.fetchHealthLakeMapping(trialId, controller.signal)
      .then((d) => { if (!controller.signal.aborted) setData(d); })
      .catch(() => {})
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [trialId]);

  if (loading) return (
    <div className={styles.loading}><Loader2 size={18} className={styles.spin} /> Loading mapping…</div>
  );
  if (!data) return <div className={styles.empty}>No mapping data available</div>;

  const toggle = (rt: string) => setExpanded((prev) => {
    const next = new Set(prev);
    if (next.has(rt)) { next.delete(rt); } else { next.add(rt); }
    return next;
  });

  return (
    <div className={styles.mappingList}>
      {data.resources.map((res) => {
        const isOpen = expanded.has(res.resourceType);
        return (
          <div key={res.resourceType} className={styles.mappingResource}>
            <button className={styles.mappingResourceHeader} onClick={() => toggle(res.resourceType)}>
              <span className={styles.mappingChevron}>{isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
              <span className={styles.mappingResourceType}>{res.resourceType}</span>
              <span className={styles.mappingResourceDesc}>{res.description}</span>
              <span className={`${styles.mappingStatus} ${styles[`mappingStatus_${res.status}`] ?? ""}`}>{res.status}</span>
            </button>
            {isOpen && (
              <div className={styles.mappingResourceBody}>
                {res.fields?.map((f, i) => (
                  <div key={i} className={styles.mappingField}>
                    <span className={styles.mappingFieldName}>{f.field}</span>
                    <span className={styles.mappingFieldPath}>{f.path}</span>
                    <span className={styles.mappingFieldValue}>{f.value || "—"}</span>
                  </div>
                ))}
                {res.fhirPaths && Object.entries(res.fhirPaths).map(([rt, rules]) => (
                  <div key={rt} className={styles.mappingFhirGroup}>
                    <div className={styles.mappingFhirGroupTitle}>{rt}</div>
                    {(rules as { ruleId: string; description: string; fhirPath: string; type: string }[]).map((r) => (
                      <div key={r.ruleId} className={styles.mappingField}>
                        <span className={styles.mappingFieldName}>{r.description}</span>
                        <span className={styles.mappingFieldPath}>{r.fhirPath}</span>
                        <Badge variant={r.type === "inclusion" ? "eligible" : "ineligible"}>{r.type}</Badge>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Helper ── */
function MetaCard({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metaCard}>
      <div className={styles.metaLabel}>{label}</div>
      <div className={styles.metaValue}>{value}</div>
    </div>
  );
}
