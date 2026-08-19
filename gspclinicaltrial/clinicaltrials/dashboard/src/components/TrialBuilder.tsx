import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import DOMPurify from 'dompurify';
import {
  Send,
  FlaskConical,
  Check,
  Loader2,
  AlertCircle,
  GripVertical,
  Link2,
  ShieldCheck,
  ShieldX,
  ClipboardList,
  Scale,
  ArrowRight,
} from "lucide-react";
import type {
  ChatMessage,
  QuestionnaireItem,
  ExtractedTrial,
  BuilderResponse,
  CreateTrialResponse,
} from "../types";
import * as api from "../api";
import styles from "./TrialBuilder.module.css";

type CreateStatus = "idle" | "creating" | "success" | "error";

/* ── Flow steps for the builder pipeline ── */
const FLOW_STEPS = [
  { id: "describe", label: "Describe Trial", icon: FlaskConical },
  { id: "extract", label: "Extract Criteria", icon: ClipboardList },
  { id: "review", label: "Review & Edit", icon: Scale },
  { id: "create", label: "Create Trial", icon: Check },
] as const;

type FlowStepId = (typeof FLOW_STEPS)[number]["id"];


export default function TrialBuilder({ onTrialCreated, ctgImport, onCtgImportConsumed }: {
  onTrialCreated: () => void;
  ctgImport?: import("../api").CtgImportResult | null;
  onCtgImportConsumed?: () => void;
}) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [extracted, setExtracted] = useState<ExtractedTrial | null>(null);
  const [items, setItems] = useState<QuestionnaireItem[]>([]);
  const [createStatus, setCreateStatus] = useState<CreateStatus>("idle");
  const [createdTrialId, setCreatedTrialId] = useState<string | null>(null);
  const [activeItemId, setActiveItemId] = useState<string | null>(null);

  const chatEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  /* ── Derived: current flow step ── */
  const currentStep: FlowStepId = useMemo(() => {
    if (createStatus === "success") return "create";
    if (items.length > 0) return "review";
    if (extracted) return "extract";
    return "describe";
  }, [createStatus, items.length, extracted]);

  /* ── Derived: grouped items by inclusion/exclusion only ── */
  const groupedItems = useMemo(() => {
    const inclusion: (QuestionnaireItem & { _idx: number })[] = [];
    const exclusion: (QuestionnaireItem & { _idx: number })[] = [];
    const other: (QuestionnaireItem & { _idx: number })[] = [];

    items.forEach((it, idx) => {
      const cat = it.category || "general";
      const entry = { ...it, _idx: idx };
      if (cat === "inclusion") inclusion.push(entry);
      else if (cat === "exclusion") exclusion.push(entry);
      else other.push(entry);
    });

    const groups: { label: string; color: string; icon: typeof ShieldCheck; items: (QuestionnaireItem & { _idx: number })[] }[] = [];
    if (inclusion.length > 0) groups.push({ label: "Inclusion Criteria", color: "var(--green)", icon: ShieldCheck, items: inclusion });
    if (exclusion.length > 0) groups.push({ label: "Exclusion Criteria", color: "var(--red)", icon: ShieldX, items: exclusion });
    if (other.length > 0) groups.push({ label: "Other Criteria", color: "var(--cyan)", icon: ClipboardList, items: other });
    return groups;
  }, [items]);

  // Auto-start session
  useEffect(() => {
    if (!sessionId) {
      api.startBuilderSession().then((data) => {
        setSessionId(data.session_id);
        setMessages(data.messages);
      }).catch(() => {
        setMessages([{ role: "assistant", content: "Unable to connect to the builder service. Please refresh and try again." }]);
      });
    }
  }, [sessionId]);

  // Consume CTG import data
  useEffect(() => {
    if (!ctgImport || !ctgImport.builderSessionId) return;

    setSessionId(ctgImport.builderSessionId);

    setMessages([
      { role: "system", content: `Imported ${ctgImport.nctId} from ClinicalTrials.gov` },
      { role: "user", content: `Import trial ${ctgImport.nctId}: ${ctgImport.title}` },
    ]);

    if (ctgImport.extracted) {
      setExtracted(ctgImport.extracted);
      setMessages((prev) => [
        ...prev,
        { role: "system", content: "Trial details extracted from ClinicalTrials.gov. Review the questionnaire and analytics." },
      ]);
    }

    if (ctgImport.questionnaireItems) {
      setItems(ctgImport.questionnaireItems.map((it) => ({ ...it, _excluded: false })));
    }

    onCtgImportConsumed?.();
  }, [ctgImport, onCtgImportConsumed]);

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(async () => {
    const msg = input.trim();
    if (!msg || !sessionId || sending) return;

    setInput("");
    setSending(true);
    setMessages((prev) => [...prev, { role: "user", content: msg }]);

    try {
      const data: BuilderResponse = await api.sendBuilderMessage(sessionId, msg);
      const agentMsgs = data.messages.filter((m) => m.role === "assistant");
      setMessages((prev) => [...prev, ...agentMsgs]);

      if (data.status === "review" && data.questionnaire_items) {
        setExtracted(data.extracted ?? null);
        setItems(data.questionnaire_items.map((it) => ({ ...it, _excluded: false })));
        setMessages((prev) => [
          ...prev,
          {
            role: "system",
            content: "Trial details extracted! Review the protocol summary and criteria table.",
          },
        ]);
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "system", content: `Error: ${err instanceof Error ? err.message : "Unknown error"}` },
      ]);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }, [input, sessionId, sending]);

  const toggleItem = (idx: number) => {
    setItems((prev) => prev.map((it, i) => (i === idx ? { ...it, _excluded: !it._excluded } : it)));
  };

  const updateItemText = (idx: number, text: string) => {
    setItems((prev) => prev.map((it, i) => (i === idx ? { ...it, text } : it)));
  };

  const handleCreate = async () => {
    if (!sessionId || createStatus === "creating") return;
    setCreateStatus("creating");

    const approved = items.filter((it) => !it._excluded);
    try {
      const data: CreateTrialResponse = await api.createTrial(sessionId, approved);
      if (data.status === "created") {
        setCreateStatus("success");
        setCreatedTrialId(data.trial_id);
        setMessages((prev) => [
          ...prev,
          { role: "system", content: `Trial ${data.trial_id} created with ${data.questionnaire_items} questionnaire items.` },
        ]);
        onTrialCreated();
      } else {
        setCreateStatus("error");
      }
    } catch {
      setCreateStatus("error");
    }
  };

  const hasData = extracted !== null || items.length > 0;

  /* ── Flow stepper (always shown at top) ── */
  const stepperEl = (
    <div className={styles.stepper}>
      {FLOW_STEPS.map((step, i) => {
        const StepIcon = step.icon;
        const stepIdx = FLOW_STEPS.findIndex((s) => s.id === currentStep);
        const thisIdx = i;
        const isDone = thisIdx < stepIdx;
        const isActive = thisIdx === stepIdx;

        return (
          <div key={step.id} className={styles.stepRow}>
            <div className={`${styles.stepDot} ${isDone ? styles.stepDone : ""} ${isActive ? styles.stepActive : ""}`}>
              {isDone ? <Check size={12} /> : <StepIcon size={12} />}
            </div>
            <span className={`${styles.stepLabel} ${isActive ? styles.stepLabelActive : ""} ${isDone ? styles.stepLabelDone : ""}`}>
              {step.label}
            </span>
            {i < FLOW_STEPS.length - 1 && <ArrowRight size={12} className={styles.stepArrow} />}
          </div>
        );
      })}
    </div>
  );

  /* ── Chat panel (right column when data exists, full-width when describe step) ── */
  const chatPanelEl = (
    <div className={hasData ? styles.chatPanel : styles.chatPanelFull}>
      <div className={styles.panelHeader}>Builder Chat</div>

      <div className={styles.chatArea}>
        {messages.length === 0 ? (
          <div className={styles.empty}>
            {!hasData && (
              <div className={styles.describePrompt}>
                <FlaskConical size={48} strokeWidth={1} />
                <p>Describe your clinical trial to get started.</p>
                <p className={styles.describeHint}>
                  Include the condition being studied, target patient population, phase, and key eligibility requirements.
                </p>
              </div>
            )}
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={i} className={`${styles.bubble} ${styles[msg.role]} animate-in`}>
              {msg.role === "assistant" ? (
                <div
                  dangerouslySetInnerHTML={{
                    __html: DOMPurify.sanitize(
                      msg.content
                        .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
                        .replace(/```json[\s\S]*?```/g, '<span class="' + styles.extractedTag + '">✓ Trial details extracted</span>')
                    ),
                  }}
                />
              ) : (
                <div>{msg.content}</div>
              )}
            </div>
          ))
        )}
        <div ref={chatEndRef} />
      </div>

      <div className={styles.inputBar}>
        <input
          ref={inputRef}
          type="text"
          className={styles.chatInput}
          placeholder="Describe your clinical trial…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSend()}
          disabled={sending}
          aria-label="Trial builder message input"
        />
        <button className={styles.sendBtn} onClick={handleSend} disabled={sending || !input.trim()} aria-label="Send message">
          {sending ? <Loader2 size={16} className={styles.spin} /> : <Send size={16} />}
        </button>
      </div>
    </div>
  );

  if (!hasData) {
    return (
      <div className={styles.layoutDescribe}>
        {stepperEl}
        <div className={styles.describeContent}>
          {chatPanelEl}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.layoutFull}>
      {stepperEl}
      <div className={styles.threeCol}>
        {/* ── Left: Protocol Summary ── */}
        <div className={styles.protocolPanel}>
          <div className={styles.panelHeader}>Protocol Summary</div>
          <div className={styles.protocolArea}>
            {extracted ? (
              <>
                {(createdTrialId || extracted.trial_id) && (
                  <div className={styles.protocolField}>
                    <span className={styles.protocolFieldLabel}>Trial ID</span>
                    <span className={`${styles.protocolFieldValue} ${styles.mono}`}>
                      {createdTrialId || extracted.trial_id}
                    </span>
                  </div>
                )}
                <div className={styles.protocolTitle}>{extracted.title || "New Trial"}</div>

                {extracted.phase && (
                  <div className={styles.protocolField}>
                    <span className={styles.protocolFieldLabel}>Phase</span>
                    <span className={styles.protocolBadge}>{extracted.phase}</span>
                  </div>
                )}

                {typeof extracted.blinded === "boolean" && (
                  <div className={styles.protocolField}>
                    <span className={styles.protocolFieldLabel}>Blinded</span>
                    <span className={extracted.blinded ? styles.protocolBadgeGreen : styles.protocolBadgeGray}>
                      {extracted.blinded ? "Yes" : "No"}
                    </span>
                  </div>
                )}

                {(extracted.age_min != null || extracted.age_max != null) && (
                  <div className={styles.protocolField}>
                    <span className={styles.protocolFieldLabel}>Age Range</span>
                    <span className={styles.protocolFieldValue}>
                      {extracted.age_min != null ? `${extracted.age_min}` : "Any"}
                      {" – "}
                      {extracted.age_max != null ? `${extracted.age_max}` : "Any"}
                      {" years"}
                    </span>
                  </div>
                )}

                {extracted.conditions?.length > 0 && (
                  <div className={styles.protocolSection}>
                    <div className={styles.protocolSectionLabel}>Conditions</div>
                    <ul className={styles.protocolList}>
                      {extracted.conditions.map((c, i) => (
                        <li key={i}>{c}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {extracted.interventions?.length > 0 && (
                  <div className={styles.protocolSection}>
                    <div className={styles.protocolSectionLabel}>Interventions</div>
                    <ul className={styles.protocolList}>
                      {extracted.interventions.map((v, i) => (
                        <li key={i}>{v}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <div className={styles.protocolEmpty}>
                Protocol details will appear here once criteria are extracted.
              </div>
            )}
          </div>
        </div>

        {/* ── Center: Criteria Table ── */}
        <div className={styles.criteriaPanel}>
          <div className={styles.panelHeader}>
            <span>Eligibility Criteria</span>
            <span className={styles.count}>
              {items.filter((it) => !it._excluded).length} / {items.length}
            </span>
          </div>

          <div className={styles.criteriaArea}>
            {items.length === 0 ? (
              <div className={styles.criteriaEmpty}>
                <ClipboardList size={40} strokeWidth={1} />
                <p>Criteria will appear here once the agent extracts them from your trial description.</p>
              </div>
            ) : (
              <>
                {groupedItems.map((group) => {
                  const GroupIcon = group.icon;
                  return (
                    <div key={group.label} className={styles.criteriaGroup}>
                      <div className={styles.criteriaGroupHeader}>
                        <GroupIcon size={13} style={{ color: group.color }} />
                        <span className={styles.criteriaGroupLabel} style={{ color: group.color }}>
                          {group.label}
                        </span>
                        <span className={styles.criteriaGroupCount}>
                          {group.items.filter((it) => !it._excluded).length}/{group.items.length}
                        </span>
                      </div>
                      <table className={styles.criteriaTable}>
                        <colgroup>
                          <col style={{ width: "28px" }} />
                          <col style={{ width: "90px" }} />
                          <col />
                          <col style={{ width: "70px" }} />
                        </colgroup>
                        <thead>
                          <tr>
                            <th></th>
                            <th>ID</th>
                            <th>Criterion</th>
                            <th>Flags</th>
                          </tr>
                        </thead>
                        <tbody>
                          {group.items.map((item) => (
                            <tr
                              key={item.linkId}
                              className={`${styles.criteriaRow} ${item._excluded ? styles.criteriaRowExcluded : ""} ${activeItemId === item.linkId ? styles.criteriaRowActive : ""}`}
                              onClick={() => setActiveItemId(item.linkId === activeItemId ? null : item.linkId)}
                            >
                              <td>
                                <input
                                  type="checkbox"
                                  checked={!item._excluded}
                                  onChange={(e) => { e.stopPropagation(); toggleItem(item._idx); }}
                                  className={styles.checkbox}
                                  aria-label={`Include ${item.text}`}
                                />
                              </td>
                              <td className={styles.criteriaLinkId}>
                                <GripVertical size={10} className={styles.grip} />
                                {item.linkId}
                              </td>
                              <td>
                                <input
                                  type="text"
                                  className={styles.criteriaTextInput}
                                  value={item.text}
                                  onChange={(e) => updateItemText(item._idx, e.target.value)}
                                  onClick={(e) => e.stopPropagation()}
                                  aria-label={`Edit criterion: ${item.linkId}`}
                                />
                              </td>
                              <td className={styles.criteriaFlags}>
                                {item.required && (
                                  <span className={styles.tagRequired}>req</span>
                                )}
                                {item.enableWhen && (
                                  <Link2 size={10} className={styles.tagDep} />
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  );
                })}
              </>
            )}
          </div>

          {/* Create button */}
          <div className={styles.createBar}>
            <button
              className={`${styles.createBtn} ${styles[`create_${createStatus}`]}`}
              onClick={handleCreate}
              disabled={items.length === 0 || createStatus === "creating" || createStatus === "success"}
            >
              {createStatus === "idle" && "Create Trial"}
              {createStatus === "creating" && (<><Loader2 size={16} className={styles.spin} /> Creating…</>)}
              {createStatus === "success" && (<><Check size={16} /> Trial Created — {createdTrialId}</>)}
              {createStatus === "error" && (<><AlertCircle size={16} /> Failed — Retry</>)}
            </button>
          </div>
        </div>

        {/* ── Right: Builder Chat ── */}
        {chatPanelEl}
      </div>
    </div>
  );
}
