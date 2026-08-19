import { useEffect, useRef, useState } from "react";
import { Loader2, AlertCircle, TrendingUp, Pill, Activity, User, Sparkles } from "lucide-react";
import * as api from "../api";
import type { Patient } from "../types";
import styles from "./PatientInsightsPanel.module.css";

interface PatientInsightsPanelProps {
  patient: Patient;
  compact?: boolean; // true = inside side panel (no bottom hint)
}

interface NarrativeChunk {
  Text: string;
  Evidence?: string[];
}

interface InsightsSection {
  SectionName: string;
  ClinicalNarrative: NarrativeChunk[];
}

interface InsightsData {
  PatientSummaries?: Array<{
    PatientSummary?: {
      Sections?: InsightsSection[];
      GeneratedAt?: string;
    };
  }>;
  GeneratedAt?: string;
}

const SECTION_META: Record<string, { label: string; icon: React.ReactNode; accent: string }> = {
  PATIENT_AND_ENCOUNTER_OVERVIEW: {
    label: "Clinical Overview",
    icon: <User size={12} />,
    accent: "overview",
  },
  SINCE_LAST_VISIT: {
    label: "Recent Changes",
    icon: <Activity size={12} />,
    accent: "changes",
  },
  TRENDS: {
    label: "Trends",
    icon: <TrendingUp size={12} />,
    accent: "trends",
  },
  MEDICATIONS: {
    label: "Medications",
    icon: <Pill size={12} />,
    accent: "meds",
  },
};

const SECTION_ORDER = [
  "PATIENT_AND_ENCOUNTER_OVERVIEW",
  "SINCE_LAST_VISIT",
  "TRENDS",
  "MEDICATIONS",
];

// Known subheadings to skip (repetitive section title at top of body)
const SKIP_SUBHEADINGS = new Set([
  "OVERVIEW",
  "PATIENT AND ENCOUNTER OVERVIEW",
  "SINCE LAST VISIT",
  "CHANGES SINCE LAST VISIT",
  "TRENDS",
  "CLINICAL TRENDS",
  "MEDICATIONS",
  "CURRENT MEDICATIONS",
]);

function normalizeKey(raw: string): string {
  return raw.replace(/^#+\s*/, "").replace(/\s+$/, "").toUpperCase();
}

function buildText(chunks: NarrativeChunk[]): string {
  return chunks.map((c) => c.Text).join("").trim();
}

// Strip [N] citation markers like [1], [2], [12]
function stripCitations(text: string): string {
  return text.replace(/\s*\[\d+\]/g, "");
}

// Wrap clinical values with inline spans for color
function applyMarkup(text: string): string {
  // HTML-escape raw input before applying any substitutions
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Numbers with units
  let result = escaped.replace(
    /(\b\d+\.?\d*\s*(?:%|mg\/dL|mmHg|mg|g\/dL|mmol\/L|mEq\/L|IU\/L|U\/L|bpm)\b)/gi,
    '<span class="hl-num">$1</span>'
  );
  // Flagged High / Low
  result = result.replace(
    /\b(flagged\s+(?:High|Low|Critical|Abnormal))\b/gi,
    '<span class="hl-flag">$1</span>'
  );
  // Worsening terms
  result = result.replace(
    /\b(worsening|uncontrolled|elevated|deteriorating|poorly controlled|high risk|abnormal|increased|above normal)\b/gi,
    '<span class="hl-bad">$1</span>'
  );
  // Positive terms
  result = result.replace(
    /\b(stable|controlled|improving|normal|within range|well-controlled|resolved)\b/gi,
    '<span class="hl-good">$1</span>'
  );
  return result;
}

function renderBody(rawText: string): React.ReactNode[] {
  const lines = rawText
    .split(/\n+/)
    .map((l) => stripCitations(l.trim()))
    .filter(Boolean);

  const nodes: React.ReactNode[] = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Heading lines
    if (line.startsWith("## ") || line.startsWith("# ")) {
      const heading = line.replace(/^#+\s*/, "").trim();
      if (!SKIP_SUBHEADINGS.has(heading.toUpperCase())) {
        nodes.push(
          <div key={i} className={styles.subheading}>{heading}</div>
        );
      }
      continue;
    }

    // Bullet points
    if (line.startsWith("- ") || line.startsWith("• ")) {
      const content = line.replace(/^[-•]\s*/, "");
      nodes.push(
        <div key={i} className={styles.bullet}>
          <span className={styles.dot} />
          <span dangerouslySetInnerHTML={{ __html: applyMarkup(content) }} />
        </div>
      );
      continue;
    }

    // Prose — skip lines that are just the section label repeated
    if (SKIP_SUBHEADINGS.has(line.toUpperCase())) continue;

    nodes.push(
      <p
        key={i}
        className={styles.prose}
        dangerouslySetInnerHTML={{ __html: applyMarkup(line) }}
      />
    );
  }

  return nodes;
}

function SectionCard({
  section,
}: {
  section: InsightsSection;
}) {
  const key = normalizeKey(section.SectionName);
  const meta = SECTION_META[key];
  if (!meta) return null;

  const text = buildText(section.ClinicalNarrative);
  if (!text) return null;

  const bodyNodes = renderBody(text);
  if (bodyNodes.length === 0) return null;

  return (
    <div className={`${styles.card} ${styles[meta.accent]}`}>
      <div className={styles.cardHeader}>
        <span className={styles.cardIcon}>{meta.icon}</span>
        <span className={styles.cardLabel}>{meta.label}</span>
      </div>
      <div className={styles.cardBody}>{bodyNodes}</div>
    </div>
  );
}

export default function PatientInsightsPanel({ patient, compact = false }: PatientInsightsPanelProps) {
  const [insights, setInsights] = useState<InsightsData | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    // Cancel any in-flight request for a prior patient
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    Promise.resolve().then(() => {
      setLoading(true);
      setNotFound(false);
      setInsights(null);
    });

    api.fetchPatientInsights(patient.id).then((data) => {
      if (controller.signal.aborted) return;
      if (data.insights) {
        setInsights(data.insights as InsightsData);
        setGeneratedAt(data.generated_at);
      } else {
        setNotFound(true);
      }
      setLoading(false);
    }).catch(() => {
      if (controller.signal.aborted) return;
      setNotFound(true);
      setLoading(false);
    });

    return () => controller.abort();
  }, [patient.id]);

  const sections: InsightsSection[] =
    insights?.PatientSummaries?.[0]?.PatientSummary?.Sections ?? [];

  const displaySections = SECTION_ORDER
    .map((k) => sections.find((s) => normalizeKey(s.SectionName) === k))
    .filter((s): s is InsightsSection => !!s);

  // Fallback: show all non-HCC sections if none matched
  const finalSections =
    displaySections.length > 0
      ? displaySections
      : sections.filter((s) => !normalizeKey(s.SectionName).includes("HCC"));

  return (
    <div className={`${styles.panel} ${compact ? styles.compact : ""}`}>
      {loading ? (
        <div className={styles.stateRow}>
          <Loader2 size={15} className={styles.spin} />
          <span>Loading AI insights…</span>
        </div>
      ) : notFound ? (
        <div className={styles.stateRow}>
          <AlertCircle size={15} className={styles.iconMuted} />
          <span>Insights generating — check back shortly</span>
        </div>
      ) : (
        <>
          <div className={styles.badge}>
            <Sparkles size={11} />
            <span>AI Assessment</span>
            {generatedAt && (
              <span className={styles.badgeDate}>
                · {new Date(generatedAt).toLocaleDateString()}
              </span>
            )}
          </div>

          <div className={styles.sections}>
            {finalSections.map((s, i) => (
              <SectionCard key={i} section={s} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
