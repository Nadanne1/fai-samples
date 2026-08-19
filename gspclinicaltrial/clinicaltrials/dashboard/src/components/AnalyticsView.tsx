import { useEffect, useState, useRef } from "react";
import { BarChart3, Loader2, X } from "lucide-react";
import type { AnalyticsData } from "../types";
import * as api from "../api";
import Badge from "./Badge";
import styles from "./AnalyticsView.module.css";

type DetermFilter = "" | "eligible" | "ineligible" | "borderline";

// ─── Main Component ──────────────────────────────────────────────────────────

export default function AnalyticsView({ onOpenTrial, onOpenPatient }: { onOpenTrial?: (trialId: string) => void; onOpenPatient?: (patientId: string) => void }) {
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [filterTrial, setFilterTrial] = useState("");
  const [filterDetermination, setFilterDetermination] = useState<DetermFilter>("");
  const screeningsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    api.fetchAnalytics()
      .then((d) => { if (active) setData(d); })
      .catch(() => { if (active) setData(null); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  if (loading) {
    return (
      <div className={styles.wrapper}>
        <div className={styles.loading}>
          <Loader2 size={24} className={styles.spin} />
          Loading analytics...
        </div>
      </div>
    );
  }

  if (!data) return <div className={styles.wrapper} />;

  const { by_determination, by_trial, recent_screenings } = data;

  // Click a stat card: filter recent screenings by determination and scroll to table
  const handleStatClick = (det: DetermFilter) => {
    setFilterDetermination((prev) => prev === det ? "" : det);
    setTimeout(() => screeningsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  const filteredScreenings = recent_screenings.filter((s) => {
    if (filterTrial && s.trial_id !== filterTrial) return false;
    if (filterDetermination && s.determination !== filterDetermination) return false;
    return true;
  });

  const filteredByTrial = filterTrial
    ? Object.fromEntries(Object.entries(by_trial).filter(([k]) => k === filterTrial))
    : by_trial;

  const maxBar = Math.max(
    by_determination.eligible || 0,
    by_determination.ineligible || 0,
    by_determination.borderline || 0,
    1
  );

  const activeFilters = [filterTrial, filterDetermination].filter(Boolean);

  return (
    <div className={styles.wrapper}>
      <div className={styles.container}>
        {/* Filter bar */}
        <div className={styles.filterBar}>
          <label htmlFor="trial-filter" className={styles.filterLabel}>Trial:</label>
          <select
            id="trial-filter"
            className={styles.filterSelect}
            value={filterTrial}
            onChange={(e) => setFilterTrial(e.target.value)}
          >
            <option value="">All Trials</option>
            {Object.keys(by_trial).map((trialId) => (
              <option key={trialId} value={trialId}>{trialId}</option>
            ))}
          </select>

          <label htmlFor="det-filter" className={styles.filterLabel}>Result:</label>
          <select
            id="det-filter"
            className={styles.filterSelect}
            value={filterDetermination}
            onChange={(e) => setFilterDetermination(e.target.value as DetermFilter)}
            style={{ minWidth: 130 }}
          >
            <option value="">All Results</option>
            <option value="eligible">Eligible</option>
            <option value="ineligible">Ineligible</option>
            <option value="borderline">Borderline</option>
          </select>

          {activeFilters.length > 0 && (
            <button
              className={styles.clearFiltersBtn}
              onClick={() => { setFilterTrial(""); setFilterDetermination(""); }}
            >
              <X size={12} /> Clear filters
            </button>
          )}
        </div>

        {/* 7-stat grid — clickable cards filter the recent screenings table */}
        <div className={styles.statGrid}>
          <div className={styles.statCard}>
            <div className={styles.statLabel}>Total Screenings</div>
            <div className={styles.statValue}>{data.total_screenings}</div>
          </div>
          <div className={styles.statCard}>
            <div className={styles.statLabel}>Unique Patients</div>
            <div className={styles.statValue}>{data.unique_patients ?? 0}</div>
          </div>
          <button
            className={`${styles.statCard} ${styles.statCardBtn} ${styles.statGreen} ${filterDetermination === "eligible" ? styles.statCardActive : ""}`}
            onClick={() => handleStatClick("eligible")}
          >
            <div className={styles.statLabel}>Eligible ↓</div>
            <div className={styles.statValue} style={{ color: "var(--green)" }}>
              {by_determination.eligible || 0}
            </div>
          </button>
          <button
            className={`${styles.statCard} ${styles.statCardBtn} ${styles.statRed} ${filterDetermination === "ineligible" ? styles.statCardActive : ""}`}
            onClick={() => handleStatClick("ineligible")}
          >
            <div className={styles.statLabel}>Ineligible ↓</div>
            <div className={styles.statValue} style={{ color: "var(--red)" }}>
              {by_determination.ineligible || 0}
            </div>
          </button>
          <button
            className={`${styles.statCard} ${styles.statCardBtn} ${styles.statYellow} ${filterDetermination === "borderline" ? styles.statCardActive : ""}`}
            onClick={() => handleStatClick("borderline")}
          >
            <div className={styles.statLabel}>Borderline ↓</div>
            <div className={styles.statValue} style={{ color: "var(--yellow)" }}>
              {by_determination.borderline || 0}
            </div>
          </button>
          <div className={styles.statCard}>
            <div className={styles.statLabel}>Queue Depth</div>
            <div className={styles.statValue}>{data.queue_depth}</div>
          </div>
          <div className={styles.statCard}>
            <div className={styles.statLabel}>Audit Records</div>
            <div className={styles.statValue}>{data.audit_records}</div>
          </div>
        </div>

        <div className={styles.twoCol}>
          <div className={styles.panel}>
            <div className={styles.panelTitle}>
              <BarChart3 size={14} />
              Eligibility Distribution
            </div>
            <div className={styles.barChart}>
              {(["eligible", "ineligible", "borderline"] as const).map((key) => {
                const val = by_determination[key] || 0;
                const pct = Math.max((val / maxBar) * 100, 4);
                const colors: Record<string, string> = {
                  eligible: "var(--green)",
                  ineligible: "var(--red)",
                  borderline: "var(--yellow)",
                };
                return (
                  <button
                    key={key}
                    className={`${styles.barGroup} ${styles.barGroupBtn} ${filterDetermination === key ? styles.barGroupActive : ""}`}
                    onClick={() => handleStatClick(key as DetermFilter)}
                  >
                    <div className={styles.barLabel}>{key}</div>
                    <div className={styles.barTrack}>
                      <div
                        className={styles.barFill}
                        style={{ width: `${pct}%`, background: colors[key] }}
                      />
                    </div>
                    <div className={styles.barCount}>{val}</div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className={styles.panel}>
            <div className={styles.panelTitle}>By Trial</div>
            {Object.keys(filteredByTrial).length === 0 ? (
              <div className={styles.emptyPanel}>No trial data</div>
            ) : (
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Trial</th>
                    <th>Total</th>
                    <th>Eligible</th>
                    <th>Ineligible</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(filteredByTrial).map(([trialId, stats]) => (
                    <tr key={trialId}>
                      <td>
                        <button
                          className={styles.trialLink}
                          onClick={() => onOpenTrial?.(trialId)}
                        >
                          {trialId}
                        </button>
                        {stats.title ? (
                          <div className={styles.trialTitle}>{stats.title}</div>
                        ) : null}
                      </td>
                      <td>{stats.total}</td>
                      <td style={{ color: "var(--green)" }}>{stats.eligible || 0}</td>
                      <td style={{ color: "var(--red)" }}>{stats.ineligible || 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Recent screenings — filtered by trial + determination */}
        <div className={styles.panel} ref={screeningsRef}>
          <div className={styles.panelTitle}>
            Recent Screenings
            {activeFilters.length > 0 && (
              <span className={styles.filterBadge}>
                {filteredScreenings.length} filtered
              </span>
            )}
          </div>
          {filteredScreenings.length === 0 ? (
            <div className={styles.emptyPanel}>No screenings match the current filters</div>
          ) : (
            <div className={styles.screeningRows}>
              {filteredScreenings.map((s) => {
                const det = s.determination as string;
                const variant =
                  det === "eligible" ? "eligible"
                  : det === "ineligible" ? "ineligible"
                  : det === "borderline" ? "borderline"
                  : "neutral";
                return (
                  <div key={s.id} className={styles.screeningRow}>
                    <div className={styles.screeningDate}>
                      {s.authored ? new Date(s.authored).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "—"}
                    </div>
                    <button className={styles.trialLink} onClick={() => onOpenPatient?.(s.patient_id)}>
                      {s.patient_id.slice(0, 14)}…
                    </button>
                    <button className={`${styles.trialLink} ${styles.mono}`} onClick={() => onOpenTrial?.(s.trial_id)}>
                      {s.trial_id}
                    </button>
                    <Badge variant={variant}>{det}</Badge>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
