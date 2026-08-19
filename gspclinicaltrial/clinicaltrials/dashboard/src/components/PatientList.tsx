import { useState } from "react";
import { Search, Info, Zap } from "lucide-react";
import type { Patient, QueueMessage } from "../types";
import Badge from "./Badge";
import styles from "./PatientList.module.css";

interface PatientListProps {
  patients: Patient[];
  queueMap: Record<string, QueueMessage>;
  selectedPatientId: string | null;
  onSelectPatient: (patient: Patient) => void;
  onOpenPatientDetail: (patientId: string) => void;
}

function getAge(birthDate: string): string {
  if (!birthDate) return "?";
  const diff = Date.now() - new Date(birthDate).getTime();
  return String(Math.floor(diff / 31557600000));
}

export default function PatientList({
  patients,
  queueMap,
  selectedPatientId,
  onSelectPatient,
  onOpenPatientDetail,
}: PatientListProps) {
  const [search, setSearch] = useState("");

  const filtered = patients
    .filter(
      (p) =>
        !search ||
        p.name.toLowerCase().includes(search.toLowerCase()) ||
        p.id.includes(search)
    )
    .sort((a, b) => {
      const aq = queueMap[a.id] ? 1 : 0;
      const bq = queueMap[b.id] ? 1 : 0;
      if (aq !== bq) return bq - aq;
      return a.name.localeCompare(b.name);
    });

  return (
    <aside className={styles.panel} aria-label="Patient list">
      <div className={styles.panelHeader}>
        <span>Patients</span>
        <span className={styles.count}>{filtered.length}</span>
      </div>

      <div className={styles.searchWrap}>
        <Search size={14} className={styles.searchIcon} />
        <input
          type="search"
          className={styles.searchInput}
          placeholder="Search patients…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search patients"
        />
      </div>

      <div className={styles.list} role="listbox" aria-label="Patients">
        {filtered.map((p) => {
          const q = queueMap[p.id];
          const isSelected = selectedPatientId === p.id;
          const isUrgent = q?.priority === "urgent";
          const avatarClass = p.gender === "female" ? styles.avatarFemale : styles.avatarMale;

          return (
            <div
              key={p.id}
              role="option"
              aria-selected={isSelected}
              className={`${styles.item} ${isSelected ? styles.itemSelected : ""} ${q ? styles.itemQueued : ""} ${isUrgent ? styles.itemUrgent : ""}`}
              onClick={() => onSelectPatient(p)}
              onKeyDown={(e) => e.key === "Enter" && onSelectPatient(p)}
              tabIndex={0}
            >
              <div className={`${styles.avatar} ${avatarClass}`}>
                {p.name.charAt(0).toUpperCase()}
              </div>

              <div className={styles.info}>
                <div className={styles.name}>{p.name || p.id.slice(0, 8)}</div>
                <div className={styles.meta}>
                  {p.gender} · {getAge(p.birthDate)}y · {p.id.slice(0, 8)}…
                </div>
              </div>

              {q && (
                <Badge variant={isUrgent ? "urgent" : "queued"}>
                  {isUrgent && <Zap size={10} />}
                  {isUrgent ? "URGENT" : "QUEUED"}
                </Badge>
              )}

              <button
                className={styles.infoBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenPatientDetail(p.id);
                }}
                aria-label={`View details for ${p.name}`}
              >
                <Info size={14} />
              </button>
            </div>
          );
        })}

        {filtered.length === 0 && (
          <div className={styles.empty}>No patients found</div>
        )}
      </div>
    </aside>
  );
}
