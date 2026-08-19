import { useEffect, useState } from "react";
import {
  Loader2,
  FlaskConical,
  Search,
  ShieldCheck,
  ShieldX,
  Calendar,
  FileText,
  ExternalLink,
  Trash2,
} from "lucide-react";
import Badge from "./Badge";
import * as api from "../api";
import styles from "./TrialsList.module.css";

interface Trial {
  trialId: string;
  title: string;
  phase: string;
  status: string;
  conditions: string[];
  interventions?: string[];
  questionnaireId: string;
  ruleCount?: number;
  inclusionCount?: number;
  exclusionCount?: number;
  createdAt?: string;
  updatedAt?: string;
}

interface TrialsListProps {
  onOpenTrial: (trialId: string) => void;
  refreshKey: number;
  onTrialDeleted: () => void;
}

export default function TrialsList({ onOpenTrial, refreshKey, onTrialDeleted }: TrialsListProps) {
  const [trials, setTrials] = useState<Trial[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  useEffect(() => {
    api.fetchTrials().then((d) => {
      setTrials(d.trials as Trial[]);
    }).catch(() => {
      setTrials([]);
    }).finally(() => {
      setLoading(false);
    });
  }, [refreshKey]);

  const handleDelete = async (trialId: string) => {
    if (!confirm(`Delete trial ${trialId}? This cannot be undone.`)) return;
    setDeleting(trialId);
    try {
      await api.deleteTrial(trialId);
      setTrials((prev) => prev.filter((t) => t.trialId !== trialId));
      onTrialDeleted();
    } catch {
      alert('Failed to delete trial. Please try again.');
    } finally {
      setDeleting(null);
    }
  };

  const filtered = trials.filter(
    (t) =>
      !search ||
      t.trialId.toLowerCase().includes(search.toLowerCase()) ||
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.conditions.some((c) => c.toLowerCase().includes(search.toLowerCase()))
  );

  if (loading) {
    return (
      <div className={styles.loading}>
        <Loader2 size={20} className={styles.spin} />
        Loading trials…
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {/* Header */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <h3 className={styles.headerTitle}>My Trials</h3>
          <span className={styles.headerCount}>{trials.length} trials</span>
        </div>
        <div className={styles.searchWrap}>
          <Search size={14} className={styles.searchIcon} />
          <input
            type="search"
            className={styles.searchInput}
            placeholder="Search trials…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search trials"
          />
        </div>
      </div>

      {/* Trials grid */}
      <div className={styles.grid}>
        {filtered.length === 0 ? (
          <div className={styles.empty}>
            <FlaskConical size={40} strokeWidth={1} />
            <p>{search ? "No trials match your search" : "No trials created yet. Use the Build tab or import from ClinicalTrials.gov."}</p>
          </div>
        ) : (
          filtered.map((trial) => (
            <div key={trial.trialId} className={styles.card}>
              <div className={styles.cardHeader}>
                <span className={styles.trialId}>{trial.trialId}</span>
                <Badge variant={trial.status === "active" ? "eligible" : "neutral"}>
                  {trial.status}
                </Badge>
              </div>

              <div className={styles.cardTitle}>{trial.title || "Untitled Trial"}</div>

              <div className={styles.cardMeta}>
                {trial.phase && <Badge variant="info">{trial.phase}</Badge>}
                {trial.conditions.slice(0, 2).map((c, i) => (
                  <span key={i} className={styles.conditionChip}>{c}</span>
                ))}
              </div>

              {/* Stats row */}
              <div className={styles.statsRow}>
                {trial.ruleCount != null && (
                  <div className={styles.stat}>
                    <FileText size={12} />
                    <span>{trial.ruleCount} criteria</span>
                  </div>
                )}
                {trial.inclusionCount != null && (
                  <div className={styles.stat}>
                    <ShieldCheck size={12} style={{ color: "var(--green)" }} />
                    <span>{trial.inclusionCount}</span>
                  </div>
                )}
                {trial.exclusionCount != null && (
                  <div className={styles.stat}>
                    <ShieldX size={12} style={{ color: "var(--red)" }} />
                    <span>{trial.exclusionCount}</span>
                  </div>
                )}
                {trial.createdAt && (
                  <div className={styles.stat}>
                    <Calendar size={12} />
                    <span>{new Date(trial.createdAt).toLocaleDateString()}</span>
                  </div>
                )}
              </div>

              {/* Actions */}
              <div className={styles.cardActions}>
                <button className={styles.actionBtn} onClick={() => onOpenTrial(trial.trialId)}>
                  <ExternalLink size={13} />
                  View Details
                </button>
                <button
                  className={styles.actionBtnDanger}
                  onClick={() => handleDelete(trial.trialId)}
                  disabled={deleting === trial.trialId}
                  title="Delete trial"
                >
                  {deleting === trial.trialId
                    ? <Loader2 size={13} className={styles.spin} />
                    : <Trash2 size={13} />}
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
