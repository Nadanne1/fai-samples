import React, { useState, useEffect, useCallback } from "react";
import type {
  Patient,
  QueueMessage,
  Trial,
  ChatMessage,
  ViewName,
} from "./types";
import * as api from "./api";
import Header from "./components/Header";
import PatientList from "./components/PatientList";
import ChatPanel from "./components/ChatPanel";
import PatientSidePanel from "./components/PatientSidePanel";
import type { TraceEntry } from "./components/PatientSidePanel";
import AnalyticsView from "./components/AnalyticsView";
import PatientDetailModal from "./components/PatientDetailModal";
import TrialDetailModal from "./components/TrialDetailModal";
import CtgSearchModal from "./components/CtgSearchModal";
import BuilderView from "./components/BuilderView";
import LoginPage from "./components/LoginPage";
import FlowEditor from "./components/FlowEditor";
import DevOpsView from "./components/DevOpsView";
import UsersView from "./components/UsersView";
import Cilantro from "./components/Cilantro";
import type { CtgImportResult } from "./api";
import { getSession, signOut, canAdmin } from "./services/AuthService";
import { setAccessToken } from "./api";
import type { Session } from "./services/AuthService";
import "./App.css";

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: "2rem", fontFamily: "monospace" }}>
          <h2>Something went wrong</h2>
          <pre style={{ color: "red" }}>{this.state.error.message}</pre>
          <button onClick={() => this.setState({ error: null })}>Retry</button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(() => {
    const s = getSession();
    if (s) setAccessToken(s.accessToken);
    return s;
  });

  const handleLogin = (s: Session) => {
    setAccessToken(s.accessToken);
    setSession(s);
  };

  if (!session) {
    return <LoginPage onLogin={handleLogin} />;
  }
  return <AppShell session={session} onSignOut={() => { setAccessToken(""); setSession(null); }} />;
}

function AppShell({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const handleSignOut = useCallback(() => {
    signOut();
    onSignOut();
  }, [onSignOut]);

  // Theme
  const [darkMode, setDarkMode] = useState(false);

  useEffect(() => {
    document.documentElement.setAttribute(
      "data-theme",
      darkMode ? "dark" : "light"
    );
  }, [darkMode]);

  // Navigation
  const [activeView, setActiveView] = useState<ViewName>("screening");

  // Global data
  const [patients, setPatients] = useState<Patient[]>([]);
  const [queueMap, setQueueMap] = useState<Record<string, QueueMessage>>({});
  const [trials, setTrials] = useState<Trial[]>([]);
  const [queueDepth, setQueueDepth] = useState(0);

  // Saved flows for DevOps and Users views
  const [savedFlows, setSavedFlows] = useState<{ id: string; name: string }[]>(() => {
    try {
      const raw = localStorage.getItem("flow-editor-saved-flows");
      return raw ? JSON.parse(raw) : [
        { id: "screening-default", name: "Patient-to-Trial Matching" },
        { id: "monitoring-default", name: "Candidate Follow-up" },
        { id: "recruitment-analytics", name: "Recruitment Analytics" },
      ];
    } catch { return [{ id: "screening-default", name: "Patient-to-Trial Matching" }]; }
  });

  const addFlow = useCallback((name: string) => {
    const id = `flow-${Date.now()}`;
    setSavedFlows((prev) => {
      const updated = [...prev, { id, name }];
      localStorage.setItem("flow-editor-saved-flows", JSON.stringify(updated));
      return updated;
    });
  }, []);

  const deleteFlow = useCallback((flowId: string) => {
    setSavedFlows((prev) => {
      const updated = prev.filter((f) => f.id !== flowId);
      localStorage.setItem("flow-editor-saved-flows", JSON.stringify(updated));
      return updated;
    });
  }, []);

  // Screening state
  const [selectedPatient, setSelectedPatient] = useState<Patient | null>(null);
  const [selectedTrialId, setSelectedTrialId] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatActive, setChatActive] = useState(false);
  const [inputDisabled, setInputDisabled] = useState(false);
  const [screeningComplete, setScreeningComplete] = useState(false);
  const [eligibility, setEligibility] = useState<{
    determination: string;
    criteria_results: { linkId: string; text: string; answer: unknown; result: string }[];
  } | undefined>(undefined);

  // Trace state
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [activeNode, setActiveNode] = useState<string | null>(null);
  const [completedNodes, setCompletedNodes] = useState<Set<string>>(new Set());

  // Modal state
  const [patientModalId, setPatientModalId] = useState<string | null>(null);
  const [trialModalId, setTrialModalId] = useState<string | null>(null);
  const [ctgSearchOpen, setCtgSearchOpen] = useState(false);
  const [ctgImportResult, setCtgImportResult] = useState<CtgImportResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Load initial data
  useEffect(() => {
    api.fetchPatients().then((d) => setPatients(d.patients)).catch((e: unknown) => {
      setLoadError(e instanceof Error ? e.message : "Failed to load patients");
    });
    api.fetchQueue().then((d) => {
      setQueueDepth(d.depth);
      const map: Record<string, QueueMessage> = {};
      d.messages.forEach((m) => { map[m.patientId] = m; });
      setQueueMap(map);
    }).catch(() => {});
    api.fetchTrials().then((d) => setTrials(d.trials)).catch((e: unknown) => {
      setLoadError(e instanceof Error ? e.message : "Failed to load trials");
    });
  }, []);

  const refreshTrials = useCallback(() => {
    api.fetchTrials().then((d) => setTrials(d.trials)).catch(() => {});
  }, []);

  // Patient selection
  const handleSelectPatient = useCallback(
    (patient: Patient) => {
      setSelectedPatient(patient);
      const q = queueMap[patient.id];
      if (q) {
        setSelectedTrialId(q.trialId);
      } else if (trials.length > 0) {
        // Auto-select first trial so Start Screening is immediately available
        setSelectedTrialId((prev) => prev || trials[0].trialId);
      }
    },
    [queueMap, trials]
  );

  // Trace helpers
  const addTrace = useCallback(
    (node: string, description: string, status: "active" | "done" = "done") => {
      setTraces((prev) => [
        ...prev,
        { node, description, status, time: new Date().toLocaleTimeString() },
      ]);
      if (status === "active") {
        setActiveNode(node);
      } else {
        setCompletedNodes((prev) => new Set(prev).add(node));
      }
    },
    []
  );

  // Start screening — the graph is non-interactive: full result arrives on /api/chat/start
  const handleStartScreening = useCallback(async () => {
    if (!selectedPatient || !selectedTrialId || chatActive) return;

    setChatActive(true);
    setScreeningComplete(false);
    setEligibility(undefined);
    setChatMessages([]);
    setTraces([]);
    setActiveNode(null);
    setCompletedNodes(new Set());
    setInputDisabled(true);

    // Show progress trace while graph runs (can take 30-120s)
    addTrace("load_patient", "Loading patient FHIR records…", "active");
    setChatMessages([
      {
        role: "system",
        content: `Running eligibility screening for ${selectedPatient.name} against trial ${selectedTrialId}…`,
      },
    ]);

    try {
      const data = await api.startScreeningChat(selectedPatient.id, selectedTrialId);
      setSessionId(data.session_id);

      addTrace("load_patient", "Patient FHIR data loaded", "done");
      addTrace("load_questionnaire", `Evaluated ${data.questionnaire_items ?? "?"} criteria`, "done");

      // Show all messages from the completed run
      if (data.messages.length > 0) {
        setChatMessages((prev) => [...prev, ...data.messages.map((m) => ({ ...m }))]);
      }

      // Graph is complete — set eligibility immediately
      if (data.status === "complete" && data.eligibility) {
        addTrace("resolve_eligibility", `Determination: ${data.eligibility.determination.toUpperCase()}`);
        addTrace("finalize_screening", "Screening complete", "done");
        setEligibility(data.eligibility);
        setScreeningComplete(true);
        setChatActive(false);
        setInputDisabled(true);
      } else if (data.status === "complete") {
        // Complete but no eligibility determination yet
        addTrace("finalize_screening", "Screening complete — no determination", "done");
        setScreeningComplete(true);
        setChatActive(false);
        setInputDisabled(true);
      } else {
        // Interactive mode (future) — enable input
        addTrace("ask_question", "Awaiting response", "active");
        setInputDisabled(false);
      }
    } catch (err) {
      setChatMessages((prev) => [
        ...prev,
        {
          role: "system",
          content: `Screening failed: ${err instanceof Error ? err.message : "Unknown error"}. Please check the trial configuration and try again.`,
        },
      ]);
      setChatActive(false);
      setInputDisabled(false);
    }
  }, [selectedPatient, selectedTrialId, chatActive, addTrace]);

  // Send screening response
  const handleSendMessage = useCallback(
    async (message: string) => {
      if (!sessionId) return;

      setInputDisabled(true);
      setChatMessages((prev) => [...prev, { role: "user", content: message }]);

      try {
        const data = await api.sendScreeningResponse(sessionId, message);

        if (data.discrepancy) {
          addTrace(
            "check_discrepancy",
            `${data.discrepancy.severity}: ${data.discrepancy.detail.slice(0, 60)}`,
            "active"
          );
        }

        const agentMsgs = data.messages.filter((m) => m.role === "assistant");
        setChatMessages((prev) => [...prev, ...agentMsgs]);

        if (data.progress) {
          addTrace(
            "validate_response",
            `${data.progress.answered}/${data.progress.total} answered`
          );
        }

        // Show validation agent activity in trace
        if (data.agentValidation && !data.agentValidation.skipped) {
          const va = data.agentValidation;
          const vaDesc = va.nextQuestionRepeated
            ? `Skipped duplicate question (${va.suggestedAction})`
            : va.suggestedAction === "rephrase_next"
            ? "Rephrased next question for clarity"
            : !va.extractedAnswerCorrect
            ? "Corrected answer extraction"
            : `Verified ✓ (confidence ${Math.round(va.confidence * 100)}%)`;
          addTrace("check_enable_when", `Validation: ${vaDesc}`);
        }

        if (data.status === "complete" && data.eligibility) {
          addTrace(
            "resolve_eligibility",
            `Determination: ${data.eligibility.determination.toUpperCase()}`
          );
          addTrace("finalize_screening", "Session complete", "done");
          setEligibility(data.eligibility);
          setScreeningComplete(true);
          setInputDisabled(true);
          setChatActive(false);
          setSessionId(null);
        } else {
          if (data.progress) {
            addTrace("ask_question", `Question ${data.progress.answered + 1}`, "active");
          }
          setInputDisabled(false);
        }
      } catch {
        setChatMessages((prev) => [
          ...prev,
          {
            role: "system",
            content: "Something went wrong processing your response. Don't worry — your previous answers are saved. Please try sending your response again.",
          },
        ]);
        setInputDisabled(false);
      }
    },
    [sessionId, addTrace]
  );

  return (
    <ErrorBoundary>
    <div className="app">
      <Header
        activeView={activeView}
        onViewChange={setActiveView}
        patientCount={patients.length}
        queueDepth={queueDepth}
        trialCount={trials.length}
        darkMode={darkMode}
        onToggleDarkMode={() => setDarkMode((d) => !d)}
        onOpenCtgSearch={() => setCtgSearchOpen(true)}
        userDisplayName={session.displayName}
        userRole={session.role}
        onSignOut={handleSignOut}
      />

      <main className="main">
        {loadError && (
          <div style={{ margin: "12px 16px", padding: "10px 16px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: "8px", color: "#dc2626", fontSize: "13px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>⚠ API error: {loadError}</span>
            <button onClick={() => setLoadError(null)} style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontWeight: 700, fontSize: "15px", lineHeight: 1 }}>×</button>
          </div>
        )}
        {activeView === "screening" && (
          <div className="screening-layout">
            <PatientList
              patients={patients}
              queueMap={queueMap}
              selectedPatientId={selectedPatient?.id ?? null}
              onSelectPatient={handleSelectPatient}
              onOpenPatientDetail={(id) => setPatientModalId(id)}
            />
            <ChatPanel
              messages={chatMessages}
              selectedPatient={selectedPatient}
              trials={trials}
              selectedTrialId={selectedTrialId}
              onTrialChange={setSelectedTrialId}
              onStartScreening={handleStartScreening}
              onSendMessage={handleSendMessage}
              chatActive={chatActive}
              inputDisabled={inputDisabled}
              screeningComplete={screeningComplete}
              eligibility={eligibility}
            />
            <PatientSidePanel
              selectedPatient={selectedPatient}
              traces={traces}
              activeNode={activeNode}
              completedNodes={completedNodes}
            />
          </div>
        )}

        {activeView === "analytics" && (
          <AnalyticsView
            onOpenTrial={(id) => setTrialModalId(id)}
            onOpenPatient={(id) => setPatientModalId(id)}
          />
        )}

        {activeView === "builder" && (
          <BuilderView
            onTrialCreated={refreshTrials}
            ctgImport={ctgImportResult}
            onCtgImportConsumed={() => setCtgImportResult(null)}
            onOpenTrialDetail={(id) => setTrialModalId(id)}
          />
        )}

        {activeView === "flow-editor" && (
          <FlowEditor />
        )}

        {activeView === "devops" && canAdmin(session.role) && (
          <DevOpsView flows={savedFlows} onAddFlow={addFlow} onDeleteFlow={deleteFlow} accessToken={session.accessToken} />
        )}

        {activeView === "users" && canAdmin(session.role) && (
          <UsersView flows={savedFlows} accessToken={session.accessToken} />
        )}
      </main>

      {/* Modals */}
      <PatientDetailModal
        patientId={patientModalId}
        onClose={() => setPatientModalId(null)}
      />
      <TrialDetailModal
        trialId={trialModalId}
        onClose={() => setTrialModalId(null)}
        onOpenPatient={(id) => {
          setTrialModalId(null);
          setPatientModalId(id);
        }}
      />
      <CtgSearchModal
        open={ctgSearchOpen}
        onClose={() => setCtgSearchOpen(false)}
        onImported={(result) => {
          setCtgImportResult(result);
          setActiveView("builder");
          refreshTrials();
        }}
      />
      {/* Cilantro — Screen-aware AI Assistant */}
      <Cilantro screenId={activeView} />
    </div>
    </ErrorBoundary>
  );
}
