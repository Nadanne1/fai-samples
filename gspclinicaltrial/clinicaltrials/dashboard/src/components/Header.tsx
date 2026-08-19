import { Activity, Moon, Sun, Users, ListTodo, FlaskConical, Globe, LogOut } from "lucide-react";
import type { ViewName } from "../types";
import type { Role } from "../services/AuthService";
import { canAdmin, canWrite } from "../services/AuthService";
import styles from "./Header.module.css";

interface HeaderProps {
  activeView: ViewName;
  onViewChange: (view: ViewName) => void;
  patientCount: number;
  queueDepth: number;
  trialCount: number;
  darkMode: boolean;
  onToggleDarkMode: () => void;
  onOpenCtgSearch: () => void;
  userDisplayName: string;
  userRole: Role;
  onSignOut: () => void;
}

const ALL_NAV_ITEMS: { id: ViewName; label: string; adminOnly?: boolean; writeOnly?: boolean }[] = [
  { id: "screening", label: "Screening" },
  { id: "analytics", label: "Analytics" },
  { id: "builder", label: "Trial Builder", writeOnly: true },
  { id: "flow-editor", label: "Flow Editor", writeOnly: true },
  { id: "devops", label: "Dev Ops", adminOnly: true },
  { id: "users", label: "Admin", adminOnly: true },
];

const ROLE_LABELS: Record<Role, string> = {
  admin: "Admin",
  researcher: "Researcher",
  viewer: "Viewer",
};

export default function Header({
  activeView,
  onViewChange,
  patientCount,
  queueDepth,
  trialCount,
  darkMode,
  onToggleDarkMode,
  onOpenCtgSearch,
  userDisplayName,
  userRole,
  onSignOut,
}: HeaderProps) {
  const navItems = ALL_NAV_ITEMS.filter((item) => {
    if (item.adminOnly) return canAdmin(userRole);
    if (item.writeOnly) return canWrite(userRole);
    return true;
  });

  return (
    <header className={styles.header}>
      <div className={styles.brand}>
        <FlaskConical size={20} className={styles.logo} />
        <h1 className={styles.title}>TrialMatch360</h1>
        <span className={styles.liveBadge}>
          <Activity size={10} />
          LIVE
        </span>
      </div>

      <nav className={styles.nav} role="tablist" aria-label="Main navigation">
        {navItems.map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={activeView === item.id}
            className={`${styles.navBtn} ${activeView === item.id ? styles.navBtnActive : ""}`}
            onClick={() => onViewChange(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className={styles.stats}>
        <div className={styles.stat}>
          <Users size={14} />
          <span className={styles.statLabel}>Patients</span>
          <span className={styles.statValue}>{patientCount}</span>
        </div>
        <div className={styles.stat}>
          <ListTodo size={14} />
          <span className={styles.statLabel}>Queue</span>
          <span className={styles.statValue}>{queueDepth}</span>
        </div>
        <div className={styles.stat}>
          <FlaskConical size={14} />
          <span className={styles.statLabel}>Trials</span>
          <span className={styles.statValue}>{trialCount}</span>
        </div>
      </div>

      {canWrite(userRole) && (
        <button
          className={styles.ctgBtn}
          onClick={onOpenCtgSearch}
          aria-label="Search ClinicalTrials.gov"
        >
          <Globe size={14} />
          <span className={styles.ctgLabel}>ClinicalTrials.gov</span>
        </button>
      )}

      <div className={styles.userInfo}>
        <span className={styles.userName}>{userDisplayName}</span>
        <span className={`${styles.roleBadge} ${styles[`role_${userRole}`]}`}>
          {ROLE_LABELS[userRole]}
        </span>
      </div>

      <button
        className={styles.themeToggle}
        onClick={onToggleDarkMode}
        aria-label={darkMode ? "Switch to light mode" : "Switch to dark mode"}
      >
        {darkMode ? <Sun size={16} /> : <Moon size={16} />}
      </button>

      <button
        className={styles.signOutBtn}
        onClick={onSignOut}
        aria-label="Sign out"
        title="Sign out"
      >
        <LogOut size={15} />
      </button>
    </header>
  );
}
