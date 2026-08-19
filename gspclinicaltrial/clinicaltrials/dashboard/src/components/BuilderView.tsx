import { useState, useCallback, useEffect, useRef } from "react";
import { Hammer, List, Shield } from "lucide-react";
import TrialBuilder from "./TrialBuilder";
import TrialsList from "./TrialsList";
import ScreeningRulesConfig from "./ScreeningRulesConfig";
import type { CtgImportResult } from "../api";
import styles from "./BuilderView.module.css";

type BuilderTab = "build" | "trials" | "rules";

interface BuilderViewProps {
  onTrialCreated: () => void;
  ctgImport?: CtgImportResult | null;
  onCtgImportConsumed?: () => void;
  onOpenTrialDetail: (trialId: string) => void;
}

const TABS: { id: BuilderTab; label: string; icon: typeof Hammer }[] = [
  { id: "trials", label: "My Trials", icon: List },
  { id: "rules", label: "Screening Rules", icon: Shield },
  { id: "build", label: "Build", icon: Hammer },
];

export default function BuilderView({
  onTrialCreated,
  ctgImport,
  onCtgImportConsumed,
  onOpenTrialDetail,
}: BuilderViewProps) {
  const [activeTab, setActiveTab] = useState<BuilderTab>("trials");
  const [refreshKey, setRefreshKey] = useState(0);

  const handleTrialCreated = useCallback(() => {
    onTrialCreated();
    setRefreshKey((k) => k + 1);
    setActiveTab("trials");
  }, [onTrialCreated]);

  const handleTrialDeleted = useCallback(() => {
    onTrialCreated(); // refresh parent trial list too
  }, [onTrialCreated]);

  // If CTG import arrives, switch to build tab
  const prevCtgImportRef = useRef(ctgImport);
  useEffect(() => {
    if (ctgImport && ctgImport !== prevCtgImportRef.current) {
      prevCtgImportRef.current = ctgImport;
      setActiveTab("build");
    }
  }, [ctgImport]);

  return (
    <div className={styles.container}>
      {/* Tab bar */}
      <div className={styles.tabBar}>
        {TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={`${styles.tab} ${activeTab === tab.id ? styles.tabActive : ""}`}
              onClick={() => setActiveTab(tab.id)}
              role="tab"
              aria-selected={activeTab === tab.id}
            >
              <Icon size={14} />
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div className={styles.content}>
        {activeTab === "build" && (
          <TrialBuilder
            onTrialCreated={handleTrialCreated}
            ctgImport={ctgImport}
            onCtgImportConsumed={onCtgImportConsumed}
          />
        )}

        {activeTab === "trials" && (
          <TrialsList
            onOpenTrial={onOpenTrialDetail}
            refreshKey={refreshKey}
            onTrialDeleted={handleTrialDeleted}
          />
        )}

        {activeTab === "rules" && (
          <ScreeningRulesConfig />
        )}
      </div>
    </div>
  );
}
