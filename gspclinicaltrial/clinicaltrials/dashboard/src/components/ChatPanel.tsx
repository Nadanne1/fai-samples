import { useRef, useEffect, useState } from "react";
import { Send, MessageSquare, Loader2 } from "lucide-react";
import DOMPurify from "dompurify";
import type { ChatMessage, Patient, Trial } from "../types";
import styles from "./ChatPanel.module.css";

interface ChatPanelProps {
  messages: ChatMessage[];
  selectedPatient: Patient | null;
  trials: Trial[];
  selectedTrialId: string;
  onTrialChange: (trialId: string) => void;
  onStartScreening: () => void;
  onSendMessage: (message: string) => void;
  chatActive: boolean;
  inputDisabled: boolean;
  screeningComplete: boolean;
  eligibility?: {
    determination: string;
    criteria_results: { linkId: string; text: string; answer: unknown; result: string }[];
  };
}

export default function ChatPanel({
  messages,
  selectedPatient,
  trials,
  selectedTrialId,
  onTrialChange,
  onStartScreening,
  onSendMessage,
  chatActive,
  inputDisabled,
  screeningComplete,
  eligibility,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const chatEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, eligibility]);

  useEffect(() => {
    if (chatActive && !inputDisabled) inputRef.current?.focus();
  }, [chatActive, inputDisabled]);

  const handleSend = () => {
    const msg = input.trim();
    if (!msg) return;
    setInput("");
    onSendMessage(msg);
  };

  const canStart = !!selectedPatient && !!selectedTrialId && !chatActive;

  return (
    <section className={styles.center} aria-label="Screening chat">
      {/* Trial selector bar */}
      <div className={styles.toolbar}>
        <select
          className={styles.select}
          value={selectedTrialId}
          onChange={(e) => onTrialChange(e.target.value)}
          aria-label="Select trial"
        >
          <option value="">Select trial…</option>
          {trials.map((t) => (
            <option key={t.trialId} value={t.trialId}>
              {t.trialId} — {t.title}
            </option>
          ))}
        </select>

        <button
          className={`${styles.startBtn} ${chatActive && !screeningComplete ? styles.startBtnRunning : ""}`}
          disabled={!canStart}
          onClick={onStartScreening}
          title={!selectedPatient ? "Select a patient from the left panel first" : !selectedTrialId ? "Select a trial first" : ""}
        >
          {chatActive && !screeningComplete ? (
            <><Loader2 size={13} className={styles.btnSpin} /> Running…</>
          ) : !selectedPatient ? (
            "← Select Patient"
          ) : (
            "Start Screening"
          )}
        </button>

        <span className={styles.patientLabel}>
          {selectedPatient ? selectedPatient.name : "No patient selected"}
        </span>
      </div>

      {/* Chat area */}
      <div className={styles.chatArea}>
        {messages.length === 0 && !eligibility ? (
          chatActive ? (
            <div className={styles.running}>
              <Loader2 size={32} className={styles.runSpin} />
              <p className={styles.runTitle}>Screening in progress</p>
              <p className={styles.runSub}>
                The screening workflow is evaluating {selectedPatient?.name ?? "the patient"} against all eligibility criteria.
                This takes 30–90 seconds.
              </p>
            </div>
          ) : (
            <div className={styles.empty}>
              <MessageSquare size={48} strokeWidth={1} />
              <p>
                {selectedPatient
                  ? `${selectedPatient.name} selected — choose a trial and click Start Screening`
                  : "Select a patient from the left panel to begin"}
              </p>
            </div>
          )
        ) : (
          <>
            {messages.map((msg, i) => (
              <div
                key={i}
                className={`${styles.bubble} ${styles[msg.role]} animate-in`}
              >
                {msg.role === "assistant" && (
                  <div
                    dangerouslySetInnerHTML={{
                      __html: DOMPurify.sanitize(
                        msg.content.replace(
                          /\*\*(.*?)\*\*/g,
                          "<strong>$1</strong>"
                        )
                      ),
                    }}
                  />
                )}
                {msg.role === "user" && <div>{msg.content}</div>}
                {msg.role === "system" && <div>{msg.content}</div>}
              </div>
            ))}

            {eligibility && (
              <div className={`${styles.resultCard} animate-in`}>
                <div className={styles.resultTitle}>Screening Complete</div>
                <div className={styles.resultGrid}>
                  <div className={styles.resultItem}>
                    <span className={styles.resultLabel}>Determination</span>
                    <span
                      className={`${styles.resultValue} ${styles[eligibility.determination]}`}
                    >
                      {eligibility.determination.toUpperCase()}
                    </span>
                  </div>
                  <div className={styles.resultItem}>
                    <span className={styles.resultLabel}>Criteria Passed</span>
                    <span className={styles.resultValue}>
                      {eligibility.criteria_results.filter((r) => r.result === "pass").length}
                    </span>
                  </div>
                  <div className={styles.resultItem}>
                    <span className={styles.resultLabel}>Criteria Failed</span>
                    <span
                      className={styles.resultValue}
                      style={{
                        color: eligibility.criteria_results.some((r) => r.result === "fail")
                          ? "var(--red)"
                          : "var(--green)",
                      }}
                    >
                      {eligibility.criteria_results.filter((r) => r.result === "fail").length}
                    </span>
                  </div>
                  <div className={styles.resultItem}>
                    <span className={styles.resultLabel}>Total Criteria</span>
                    <span className={styles.resultValue}>
                      {eligibility.criteria_results.length}
                    </span>
                  </div>
                </div>

                {eligibility.criteria_results.length > 0 && (
                  <div className={styles.criteriaBreakdown}>
                    <div className={styles.criteriaBreakdownTitle}>Criteria Breakdown</div>
                    <table className={styles.criteriaTable}>
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>Criterion</th>
                          <th>Result</th>
                        </tr>
                      </thead>
                      <tbody>
                        {eligibility.criteria_results.map((r) => (
                          <tr key={r.linkId}>
                            <td className={styles.criteriaLinkId}>{r.linkId}</td>
                            <td className={styles.criteriaText}>
                              {r.text.length > 60 ? r.text.slice(0, 60) + "…" : r.text}
                            </td>
                            <td>
                              <span
                                className={`${styles.criteriaBadge} ${
                                  r.result === "pass"
                                    ? styles.criteriaBadgePass
                                    : r.result === "fail"
                                    ? styles.criteriaBadgeFail
                                    : styles.criteriaBadgeIndeterminate
                                }`}
                              >
                                {r.result}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
            <div ref={chatEndRef} />
          </>
        )}
      </div>

      {/* Chat input */}
      {chatActive && (
        <div className={styles.inputBar}>
          <input
            ref={inputRef}
            type="text"
            className={styles.chatInput}
            placeholder={screeningComplete ? "Screening complete" : "Type your response…"}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSend()}
            disabled={inputDisabled || screeningComplete}
            aria-label="Chat message input"
          />
          <button
            className={styles.sendBtn}
            onClick={handleSend}
            disabled={inputDisabled || screeningComplete || !input.trim()}
            aria-label="Send message"
          >
            <Send size={16} />
          </button>
        </div>
      )}
    </section>
  );
}
