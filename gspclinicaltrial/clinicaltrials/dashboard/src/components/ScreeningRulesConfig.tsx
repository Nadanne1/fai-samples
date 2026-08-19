import { useEffect, useState, useCallback } from "react";
import {
  Loader2,
  Shield,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  Plus,
  RotateCcw,
  Trash2,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
  UserCheck,
  Users,
  FileWarning,
  Clock,
  Baby,
  Heart,
} from "lucide-react";
import Badge from "./Badge";
import type { ScreeningRule } from "../api";
import * as api from "../api";
import styles from "./ScreeningRulesConfig.module.css";

const ACTION_LABELS: Record<string, { label: string; color: string }> = {
  require_guardian_consent: { label: "Require Guardian", color: "var(--orange)" },
  escalate_to_pi: { label: "Escalate to PI", color: "var(--yellow)" },
  add_followup_question: { label: "Follow-up Questions", color: "var(--blue)" },
  flag_for_review: { label: "Flag for Review", color: "var(--red)" },
  auto_pass: { label: "Auto Pass", color: "var(--green)" },
  auto_fail: { label: "Auto Fail", color: "var(--red)" },
  conditional_pass: { label: "Conditional", color: "var(--cyan)" },
};

const CATEGORY_ICONS: Record<string, typeof Shield> = {
  age: Baby,
  consent: UserCheck,
  safety: Heart,
  eligibility: ShieldCheck,
  demographics: Users,
  data_quality: FileWarning,
  custom: Shield,
};

const TRIGGER_LABELS: Record<string, string> = {
  minor_patient: "Patient is a minor (under threshold age)",
  age_below_minimum: "Age below trial minimum",
  age_above_maximum: "Age above trial maximum",
  consent_declined: "Informed consent declined",
  critical_discrepancy: "Critical discrepancy detected",
  exclusion_borderline: "Borderline exclusion criterion",
  multiple_criteria_failures: "Multiple criteria failed",
  missing_fhir_records: "Missing FHIR records",
  gender_not_applicable: "Gender-based auto-skip",
};

const ACTION_OPTIONS = [
  { value: "require_guardian_consent", label: "Require Guardian Consent" },
  { value: "escalate_to_pi", label: "Escalate to PI" },
  { value: "add_followup_question", label: "Add Follow-up Questions" },
  { value: "flag_for_review", label: "Flag for Review" },
  { value: "auto_pass", label: "Auto Pass" },
  { value: "auto_fail", label: "Auto Fail" },
  { value: "conditional_pass", label: "Conditional Pass" },
];

const TRIGGER_OPTIONS = Object.entries(TRIGGER_LABELS).map(([value, label]) => ({ value, label }));

export default function ScreeningRulesConfig() {
  const [rules, setRules] = useState<ScreeningRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedRule, setExpandedRule] = useState<string | null>(null);
  const [showNewForm, setShowNewForm] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadRules = useCallback(() => {
    api.fetchScreeningRules().then((d) => {
      setRules(d.rules);
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadRules(); }, [loadRules]);

  const handleToggle = async (ruleId: string, enabled: boolean) => {
    try {
      await api.updateScreeningRule(ruleId, { enabled });
      setRules((prev) => prev.map((r) => (r.id === ruleId ? { ...r, enabled } : r)));
    } catch (e) {
      console.error("Failed to toggle rule:", e);
    }
  };

  const handleDelete = async (ruleId: string) => {
    try {
      await api.deleteScreeningRule(ruleId);
      setRules((prev) => prev.filter((r) => r.id !== ruleId));
    } catch (e) {
      console.error("Failed to delete rule:", e);
    }
  };

  const handleReset = async () => {
    try {
      const data = await api.resetScreeningRules();
      setRules(data.rules);
    } catch (e) {
      console.error("Failed to reset rules:", e);
    }
  };

  const handleCreate = async (rule: Omit<ScreeningRule, "id">) => {
    setCreateError(null);
    try {
      const created = await api.createScreeningRule(rule);
      setRules((prev) => [...prev, created]);
      setShowNewForm(false);
    } catch {
      setCreateError('Failed to create rule. Please try again.');
    }
  };

  if (loading) {
    return (
      <div className={styles.loading}>
        <Loader2 size={20} className={styles.spin} />
        Loading screening rules…
      </div>
    );
  }

  const enabledCount = rules.filter((r) => r.enabled).length;

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <Shield size={18} />
          <h3 className={styles.headerTitle}>Screening Rules</h3>
          <span className={styles.headerCount}>{enabledCount}/{rules.length} active</span>
        </div>
        <div className={styles.headerActions}>
          <button className={styles.actionBtn} onClick={() => setShowNewForm(true)}>
            <Plus size={14} /> Add Rule
          </button>
          <button className={styles.actionBtnMuted} onClick={handleReset}>
            <RotateCcw size={14} /> Reset Defaults
          </button>
        </div>
      </div>

      <div className={styles.description}>
        Deterministic rules that trigger special handling during patient screening.
        When a validation check fails, these rules determine the next step — such as
        requesting guardian consent for minors or escalating to the Principal Investigator.
      </div>

      {showNewForm && (
        <>
          {createError && (
            <div style={{ color: "var(--red, #ef4444)", fontSize: "13px", padding: "8px 12px", marginBottom: "8px", border: "1px solid var(--red, #ef4444)", borderRadius: "6px", background: "rgba(239,68,68,0.08)" }}>
              {createError}
            </div>
          )}
          <NewRuleForm onSave={handleCreate} onCancel={() => { setShowNewForm(false); setCreateError(null); }} />
        </>
      )}

      <div className={styles.rulesList}>
        {rules.map((rule) => {
          const isExpanded = expandedRule === rule.id;
          const CatIcon = CATEGORY_ICONS[rule.category] || Shield;
          const actionMeta = ACTION_LABELS[rule.action] || { label: rule.action, color: "var(--text2)" };

          return (
            <div key={rule.id} className={`${styles.ruleCard} ${!rule.enabled ? styles.ruleDisabled : ""}`}>
              <div className={styles.ruleHeader}>
                <button className={styles.expandBtn} onClick={() => setExpandedRule(isExpanded ? null : rule.id)}>
                  {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>

                <CatIcon size={16} style={{ color: actionMeta.color }} />

                <div className={styles.ruleInfo}>
                  <div className={styles.ruleName}>{rule.name}</div>
                  <div className={styles.ruleMeta}>
                    <span className={styles.ruleId}>{rule.id}</span>
                    <span>·</span>
                    <span>{rule.category}</span>
                    <span>·</span>
                    <span>priority {rule.priority}</span>
                  </div>
                </div>

                <Badge variant={rule.enabled ? "eligible" : "neutral"}>
                  <span style={{ color: actionMeta.color }}>{actionMeta.label}</span>
                </Badge>

                <button
                  className={styles.toggleBtn}
                  onClick={() => handleToggle(rule.id, !rule.enabled)}
                  aria-label={rule.enabled ? "Disable rule" : "Enable rule"}
                >
                  {rule.enabled
                    ? <ToggleRight size={22} style={{ color: "var(--green)" }} />
                    : <ToggleLeft size={22} style={{ color: "var(--text3)" }} />
                  }
                </button>
              </div>

              {isExpanded && (
                <div className={styles.ruleBody}>
                  <div className={styles.ruleDesc}>{rule.description}</div>

                  <div className={styles.ruleGrid}>
                    <div className={styles.ruleSection}>
                      <div className={styles.sectionTitle}>
                        <AlertTriangle size={12} /> Trigger
                      </div>
                      <div className={styles.sectionValue}>
                        {TRIGGER_LABELS[rule.trigger] || rule.trigger}
                      </div>
                      {Object.keys(rule.triggerConfig).length > 0 && (
                        <div className={styles.configChips}>
                          {Object.entries(rule.triggerConfig).map(([k, v]) => (
                            <span key={k} className={styles.configChip}>{k}: {String(v)}</span>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className={styles.ruleSection}>
                      <div className={styles.sectionTitle}>
                        <Clock size={12} /> Action
                      </div>
                      <div className={styles.sectionValue} style={{ color: actionMeta.color }}>
                        {actionMeta.label}
                      </div>
                      {(rule.actionConfig as { message?: string }).message && (
                        <div className={styles.actionMessage}>
                          "{(rule.actionConfig as { message: string }).message}"
                        </div>
                      )}
                      {(rule.actionConfig as { followupQuestions?: (string | { text: string; type: string })[] }).followupQuestions && (
                        <div className={styles.followupList}>
                          <div className={styles.followupTitle}>Follow-up questions:</div>
                          {((rule.actionConfig as { followupQuestions: (string | { text: string; type: string })[] }).followupQuestions).map((q, i) => {
                            const text = typeof q === "string" ? q : q.text;
                            const type = typeof q === "string" ? "string" : q.type;
                            return (
                              <div key={i} className={styles.followupItem}>
                                {i + 1}. {text} <span style={{ color: "var(--text3)", fontSize: 10 }}>({type})</span>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </div>

                  <div className={styles.ruleActions}>
                    <button className={styles.deleteBtn} onClick={() => handleDelete(rule.id)}>
                      <Trash2 size={13} /> Delete Rule
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function NewRuleForm({ onSave, onCancel }: { onSave: (rule: Omit<ScreeningRule, "id">) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [trigger, setTrigger] = useState(TRIGGER_OPTIONS[0].value);
  const [action, setAction] = useState(ACTION_OPTIONS[0].value);
  const [message, setMessage] = useState("");
  const [category, setCategory] = useState("custom");
  const [priority, setPriority] = useState(10);

  return (
    <div className={styles.newRuleForm}>
      <div className={styles.formTitle}>New Screening Rule</div>
      <div className={styles.formGrid}>
        <label className={styles.formField}>
          <span>Name</span>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Rule name" />
        </label>
        <label className={styles.formField}>
          <span>Category</span>
          <input type="text" value={category} onChange={(e) => setCategory(e.target.value)} placeholder="e.g. age, consent, safety" />
        </label>
        <label className={styles.formField} style={{ gridColumn: "1 / -1" }}>
          <span>Description</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What does this rule do?" rows={2} />
        </label>
        <label className={styles.formField}>
          <span>Trigger</span>
          <select value={trigger} onChange={(e) => setTrigger(e.target.value)}>
            {TRIGGER_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <label className={styles.formField}>
          <span>Action</span>
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            {ACTION_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <label className={styles.formField} style={{ gridColumn: "1 / -1" }}>
          <span>Action Message</span>
          <textarea value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Message shown to the patient when this rule triggers" rows={2} />
        </label>
        <label className={styles.formField}>
          <span>Priority</span>
          <input type="number" value={priority} onChange={(e) => setPriority(Number(e.target.value))} min={1} max={99} />
        </label>
      </div>
      <div className={styles.formActions}>
        <button className={styles.formCancel} onClick={onCancel}>Cancel</button>
        <button className={styles.formSave} onClick={() => onSave({ name, description, trigger, triggerConfig: {}, action, actionConfig: { message }, enabled: true, priority, category })} disabled={!name || !trigger || !action}>
          Create Rule
        </button>
      </div>
    </div>
  );
}
