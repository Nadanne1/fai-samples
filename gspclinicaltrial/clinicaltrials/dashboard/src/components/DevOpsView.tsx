import { useState, useCallback, useEffect, useMemo } from "react";
import { Play, Shield, Rocket, CheckCircle2, XCircle, Loader2, Zap, Terminal, Plus, Edit3, Trash2, Save, X, FlaskConical, Activity } from "lucide-react";
import Tooltip from "./Tooltip";

// ─── Types ───────────────────────────────────────────────────────────────────

interface FlowRun {
  id: string;
  flowId: string;
  flowName: string;
  action: "test" | "scan" | "deploy" | "run" | "activate";
  status: "pending" | "running" | "success" | "failed";
  startedAt: string;
  completedAt?: string;
  output?: string;
}

interface TestCase {
  flow_id: string;
  test_id: string;
  name: string;
  description: string;
  test_type: string;
  input_state: Record<string, unknown>;
  expected_output: Record<string, unknown>;
  assertions: { field: string; operator: string; value: string }[];
  tags: string[];
  enabled: boolean;
  last_result: string;
  last_run_at?: string;
  created_at: string;
  updated_at: string;
}

type DevOpsTab = "pipeline" | "tests" | "traces";

const ACTION_META: Record<string, { icon: React.ReactNode; color: string; label: string }> = {
  test: { icon: <CheckCircle2 size={14} />, color: "#22c55e", label: "Test" },
  scan: { icon: <Shield size={14} />, color: "#6366f1", label: "Security Scan" },
  deploy: { icon: <Rocket size={14} />, color: "#f59e0b", label: "Deploy" },
  run: { icon: <Play size={14} />, color: "#3b82f6", label: "Run" },
  activate: { icon: <Zap size={14} />, color: "#ec4899", label: "Activate" },
};

const API = import.meta.env.VITE_API_URL || "";
const API_KEY = import.meta.env.VITE_API_KEY || "";

// ─── Main Component ──────────────────────────────────────────────────────────

export default function DevOpsView({ flows, onAddFlow, onDeleteFlow, accessToken = "" }: { flows: { id: string; name: string }[]; onAddFlow?: (name: string) => void; onDeleteFlow?: (id: string) => void; accessToken?: string }) {
  const authHeaders = useMemo<Record<string, string>>(() => ({
    ...(API_KEY ? { "X-Api-Key": API_KEY } : {}),
    ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
  }), [accessToken]);
  const [activeTab, setActiveTab] = useState<DevOpsTab>("pipeline");
  const [selectedFlowId, setSelectedFlowId] = useState<string>(flows[0]?.id || "");
  const [runs, setRuns] = useState<FlowRun[]>([]);
  const [runningAction, setRunningAction] = useState<string | null>(null);
  const [showNewFlowInput, setShowNewFlowInput] = useState(false);
  const [newFlowName, setNewFlowName] = useState("");

  // Test state
  const [tests, setTests] = useState<TestCase[]>([]);
  const [loadingTests, setLoadingTests] = useState(false);
  const [showTestForm, setShowTestForm] = useState(false);
  const [editingTest, setEditingTest] = useState<TestCase | null>(null);
  const [runningTestId, setRunningTestId] = useState<string | null>(null);

  // Test form
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formType, setFormType] = useState("unit");
  const [formInput, setFormInput] = useState("{}");
  const [formExpected, setFormExpected] = useState("{}");
  const [formTags, setFormTags] = useState("");

  const selectedFlow = flows.find((f) => f.id === selectedFlowId);

  const loadTests = useCallback(async () => {
    if (!selectedFlowId) return;
    try {
      setLoadingTests(true);
      const res = await fetch(`${API}/api/devops/tests/${selectedFlowId}`, { headers: { ...authHeaders } });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const data = await res.json();
      setTests(data.tests || []);
    } catch (e) {
      setTests([]);
      console.error("Failed to load tests:", e);
    } finally {
      setLoadingTests(false);
    }
  }, [selectedFlowId, authHeaders]);

  // Load tests when flow changes
  useEffect(() => {
    if (selectedFlowId && activeTab === "tests") {
      loadTests(); // eslint-disable-line react-hooks/set-state-in-effect
    }
  }, [selectedFlowId, activeTab, loadTests]);

  // ─── Pipeline actions ──────────────────────────────────────────────────────

  const executeAction = useCallback(async (action: FlowRun["action"]) => {
    if (!selectedFlowId || runningAction) return;
    setRunningAction(action);
    const run: FlowRun = { id: `run-${Date.now()}`, flowId: selectedFlowId, flowName: selectedFlow?.name || selectedFlowId, action, status: "running", startedAt: new Date().toISOString() };
    setRuns((prev) => [run, ...prev]);

    await new Promise((r) => setTimeout(r, 1200));
    setRuns((prev) => prev.map((r) => r.id === run.id ? { ...r, status: "success", completedAt: new Date().toISOString(), output: `${ACTION_META[action].label} completed for demo validation` } : r));
    setRunningAction(null);
  }, [selectedFlowId, selectedFlow, runningAction]);

  // ─── Test CRUD ─────────────────────────────────────────────────────────────

  const resetForm = () => {
    setFormName(""); setFormDesc(""); setFormType("unit"); setFormInput("{}"); setFormExpected("{}"); setFormTags("");
    setEditingTest(null); setShowTestForm(false);
  };

  const openEditForm = (test: TestCase) => {
    setFormName(test.name);
    setFormDesc(test.description);
    setFormType(test.test_type);
    setFormInput(JSON.stringify(test.input_state, null, 2));
    setFormExpected(JSON.stringify(test.expected_output, null, 2));
    setFormTags(test.tags.join(", "));
    setEditingTest(test);
    setShowTestForm(true);
  };

  const saveTest = async () => {
    if (!formName.trim() || !selectedFlowId) return;
    let inputState = {};
    let expectedOutput = {};
    try { inputState = JSON.parse(formInput); } catch { /* keep empty */ }
    try { expectedOutput = JSON.parse(formExpected); } catch { /* keep empty */ }

    try {
      if (editingTest) {
        const res = await fetch(`${API}/api/devops/tests/${selectedFlowId}/${editingTest.test_id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json", ...authHeaders },
          body: JSON.stringify({ name: formName, description: formDesc, test_type: formType, input_state: inputState, expected_output: expectedOutput, tags: formTags.split(",").map((t) => t.trim()).filter(Boolean) }),
        });
        if (!res.ok) return;
      } else {
        const res = await fetch(`${API}/api/devops/tests`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders },
          body: JSON.stringify({ flow_id: selectedFlowId, name: formName, description: formDesc, test_type: formType, input_state: inputState, expected_output: expectedOutput, tags: formTags.split(",").map((t) => t.trim()).filter(Boolean) }),
        });
        if (!res.ok) return;
      }
      resetForm();
      loadTests();
    } catch (e) {
      console.error("Failed to save test:", e);
    }
  };

  const deleteTest = async (testId: string) => {
    try {
      const res = await fetch(`${API}/api/devops/tests/${selectedFlowId}/${testId}`, { method: "DELETE", headers: { ...authHeaders } });
      if (res.ok) loadTests();
    } catch (e) {
      console.error("Failed to delete test:", e);
    }
  };

  const runSingleTest = async (testId: string) => {
    setRunningTestId(testId);
    try {
      const res = await fetch(`${API}/api/devops/tests/${selectedFlowId}/${testId}/run`, { method: "POST", headers: { ...authHeaders } });
      if (res.ok) await loadTests();
    } catch (e) {
      console.error("Failed to run test:", e);
    } finally {
      setRunningTestId(null);
    }
  };

  const inputStyle: React.CSSProperties = { padding: "8px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface2)", color: "var(--text)", fontSize: "13px", outline: "none", width: "100%" };
  const monoStyle: React.CSSProperties = { ...inputStyle, fontFamily: "'SF Mono', monospace", fontSize: "11px", minHeight: "60px", resize: "vertical" };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* Header */}
      <div style={{ padding: "16px 24px", borderBottom: "1px solid var(--border)", background: "var(--surface)", display: "flex", alignItems: "center", gap: "12px", flexShrink: 0 }}>
        <Terminal size={18} style={{ color: "var(--accent)" }} />
        <h2 style={{ margin: 0, fontSize: "16px", fontWeight: 700 }}>DevOps — {selectedFlow?.name || "Select a flow"}</h2>
        <Tooltip content="DevOps Management" steps={["Select a flow from the dropdown", "Use Pipeline tab for deploy/scan/run actions", "Use Tests tab to create and manage test cases", "Cilantro can create tests via chat — try asking!"]} position="bottom" />
        <div style={{ flex: 1 }} />
        <select value={selectedFlowId} onChange={(e) => setSelectedFlowId(e.target.value)} style={{ padding: "8px 14px", borderRadius: "7px", border: "1px solid var(--border)", background: "var(--surface2)", color: "var(--text)", fontSize: "13px", minWidth: "200px" }}>
          <option value="">Select a flow...</option>
          {flows.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
        </select>
        {showNewFlowInput ? (
          <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
            <input value={newFlowName} onChange={(e) => setNewFlowName(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && newFlowName.trim()) { onAddFlow?.(newFlowName.trim()); setNewFlowName(""); setShowNewFlowInput(false); } }} placeholder="Flow name..." autoFocus style={{ padding: "8px 12px", borderRadius: "7px", border: "1px solid var(--accent)", background: "var(--surface2)", color: "var(--text)", fontSize: "13px", width: "160px", outline: "none" }} />
            <button onClick={() => { if (newFlowName.trim()) { onAddFlow?.(newFlowName.trim()); setNewFlowName(""); setShowNewFlowInput(false); } }} disabled={!newFlowName.trim()} style={{ padding: "8px 12px", borderRadius: "7px", border: "none", background: "var(--accent)", color: "white", fontSize: "12px", fontWeight: 600, cursor: "pointer", opacity: newFlowName.trim() ? 1 : 0.5 }}>Add</button>
            <button onClick={() => { setShowNewFlowInput(false); setNewFlowName(""); }} style={{ padding: "8px 10px", borderRadius: "7px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", fontSize: "12px", cursor: "pointer" }}><X size={13} /></button>
          </div>
        ) : (
          <button onClick={() => setShowNewFlowInput(true)} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "8px 14px", borderRadius: "7px", border: "1px dashed var(--border)", background: "none", color: "var(--accent)", fontSize: "12px", cursor: "pointer" }}>
            <Plus size={13} /> New Flow
          </button>
        )}
        {selectedFlowId && onDeleteFlow && (
          <button onClick={() => { onDeleteFlow(selectedFlowId); setSelectedFlowId(flows.find((f) => f.id !== selectedFlowId)?.id || ""); }} title="Delete selected flow" style={{ padding: "8px 10px", borderRadius: "7px", border: "1px solid #ef4444", background: "none", color: "#ef4444", fontSize: "12px", cursor: "pointer", display: "flex", alignItems: "center" }}>
            <Trash2 size={13} />
          </button>
        )}
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: "2px", padding: "8px 24px 0", borderBottom: "1px solid var(--border)", background: "var(--surface)", flexShrink: 0 }}>
        <button onClick={() => setActiveTab("pipeline")} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "9px 14px", border: "none", background: "none", color: activeTab === "pipeline" ? "var(--accent)" : "var(--text2)", fontSize: "12px", fontWeight: activeTab === "pipeline" ? 600 : 500, cursor: "pointer", borderBottom: `2px solid ${activeTab === "pipeline" ? "var(--accent)" : "transparent"}`, borderRadius: "6px 6px 0 0" }}>
          <Rocket size={13} /> Pipeline
        </button>
        <button onClick={() => setActiveTab("tests")} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "9px 14px", border: "none", background: "none", color: activeTab === "tests" ? "var(--accent)" : "var(--text2)", fontSize: "12px", fontWeight: activeTab === "tests" ? 600 : 500, cursor: "pointer", borderBottom: `2px solid ${activeTab === "tests" ? "var(--accent)" : "transparent"}`, borderRadius: "6px 6px 0 0" }}>
          <FlaskConical size={13} /> Tests
          {tests.length > 0 && <span style={{ fontSize: "10px", padding: "1px 5px", borderRadius: "8px", background: "var(--surface2)", color: "var(--text2)" }}>{tests.length}</span>}
        </button>
        <button onClick={() => setActiveTab("traces")} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "9px 14px", border: "none", background: "none", color: activeTab === "traces" ? "var(--accent)" : "var(--text2)", fontSize: "12px", fontWeight: activeTab === "traces" ? 600 : 500, cursor: "pointer", borderBottom: `2px solid ${activeTab === "traces" ? "var(--accent)" : "transparent"}`, borderRadius: "6px 6px 0 0" }}>
          <Activity size={13} /> Traces
        </button>
      </div>

      {/* Pipeline Tab */}
      {activeTab === "pipeline" && (
        <>
          <div style={{ margin: "12px 24px 0", background: "#fff3cd", border: "1px solid #ffc107", padding: "8px 12px", borderRadius: "4px", fontSize: "13px" }}>Pipeline actions are simulated for demo validation.</div>
          <div style={{ padding: "16px 24px", display: "flex", gap: "10px", flexWrap: "wrap", borderBottom: "1px solid var(--border)", background: "var(--surface)" }}>
            {(["test", "scan", "deploy", "run", "activate"] as const).map((action) => {
              const meta = ACTION_META[action];
              const isRunning = runningAction === action;
              return (
                <button key={action} onClick={() => executeAction(action)} disabled={!selectedFlowId || !!runningAction} style={{ display: "flex", alignItems: "center", gap: "7px", padding: "10px 18px", borderRadius: "8px", border: `1px solid ${meta.color}30`, background: `${meta.color}10`, color: meta.color, fontSize: "13px", fontWeight: 600, cursor: "pointer", opacity: (!selectedFlowId || !!runningAction) ? 0.5 : 1, transition: "all 0.15s" }}>
                  {isRunning ? <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> : meta.icon}
                  {meta.label}
                </button>
              );
            })}
          </div>
          <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
            <div style={{ fontSize: "12px", fontWeight: 600, color: "var(--text2)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: "12px" }}>Run History</div>
            {runs.length === 0 ? (
              <div style={{ textAlign: "center", padding: "60px 20px", color: "var(--text2)" }}>
                <Terminal size={36} style={{ marginBottom: "12px", opacity: 0.4 }} />
                <p style={{ fontSize: "14px" }}>No pipeline runs yet.</p>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                {runs.map((run) => { const meta = ACTION_META[run.action]; return (
                  <div key={run.id} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "12px 16px", border: "1px solid var(--border)", borderRadius: "9px", background: "var(--surface)" }}>
                    <div style={{ color: meta.color }}>{meta.icon}</div>
                    <div style={{ flex: 1 }}><div style={{ fontSize: "13px", fontWeight: 600 }}>{meta.label} — {run.flowName}</div><div style={{ fontSize: "11px", color: "var(--text2)", marginTop: "2px" }}>{run.output || "Running..."}</div></div>
                    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                      {run.status === "running" && <Loader2 size={14} style={{ color: "#3b82f6", animation: "spin 1s linear infinite" }} />}
                      {run.status === "success" && <CheckCircle2 size={14} style={{ color: "#22c55e" }} />}
                      {run.status === "failed" && <XCircle size={14} style={{ color: "#ef4444" }} />}
                      <span style={{ fontSize: "11px", color: "var(--text2)" }}>{new Date(run.startedAt).toLocaleTimeString()}</span>
                    </div>
                  </div>
                ); })}
              </div>
            )}
          </div>
        </>
      )}

      {/* Tests Tab */}
      {activeTab === "tests" && (
        <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
          {/* Actions bar */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "12px", color: "var(--text2)" }}>{tests.length} test cases</span>
              <Tooltip content="Test Management" steps={["Click 'New Test' to create a test case", "Define input state and expected output as JSON", "Run individual tests or all tests at once", "Cilantro can create tests — ask: 'Create a test for patient validation'"]} position="bottom" />
            </div>
            <div style={{ display: "flex", gap: "8px" }}>
              <button onClick={() => { resetForm(); setShowTestForm(true); }} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "8px 16px", borderRadius: "7px", border: "none", background: "var(--accent)", color: "white", fontSize: "12px", fontWeight: 600, cursor: "pointer" }}>
                <Plus size={13} /> New Test
              </button>
              <button onClick={loadTests} style={{ padding: "8px 12px", borderRadius: "7px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", fontSize: "12px", cursor: "pointer" }}>
                Refresh
              </button>
            </div>
          </div>

          {/* Test Form */}
          {showTestForm && (
            <div style={{ padding: "16px", border: "1px solid var(--border)", borderRadius: "10px", background: "var(--surface)", marginBottom: "16px", display: "flex", flexDirection: "column", gap: "12px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ fontSize: "14px", fontWeight: 600 }}>{editingTest ? "Edit Test Case" : "New Test Case"}</span>
                <button onClick={resetForm} style={{ background: "none", border: "none", color: "var(--text2)", cursor: "pointer" }}><X size={16} /></button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "10px" }}>
                <div>
                  <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Test Name</label>
                  <input style={inputStyle} value={formName} onChange={(e) => setFormName(e.target.value)} placeholder="e.g. Validate age criterion passes for adult" />
                </div>
                <div>
                  <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Type</label>
                  <select style={inputStyle} value={formType} onChange={(e) => setFormType(e.target.value)}>
                    <option value="unit">Unit</option>
                    <option value="integration">Integration</option>
                    <option value="e2e">End-to-End</option>
                    <option value="load">Load</option>
                  </select>
                </div>
              </div>
              <div>
                <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Description</label>
                <input style={inputStyle} value={formDesc} onChange={(e) => setFormDesc(e.target.value)} placeholder="What does this test verify?" />
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" }}>
                <div>
                  <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Input State (JSON)</label>
                  <textarea style={monoStyle as React.CSSProperties} value={formInput} onChange={(e) => setFormInput(e.target.value)} placeholder='{"patient_id": "...", "trial_id": "..."}' />
                </div>
                <div>
                  <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Expected Output (JSON)</label>
                  <textarea style={monoStyle as React.CSSProperties} value={formExpected} onChange={(e) => setFormExpected(e.target.value)} placeholder='{"determination": "eligible"}' />
                </div>
              </div>
              <div>
                <label style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", display: "block", marginBottom: "4px" }}>Tags (comma-separated)</label>
                <input style={inputStyle} value={formTags} onChange={(e) => setFormTags(e.target.value)} placeholder="e.g. age, eligibility, critical" />
              </div>
              <div style={{ display: "flex", gap: "8px" }}>
                <button onClick={saveTest} disabled={!formName.trim()} style={{ display: "flex", alignItems: "center", gap: "5px", padding: "8px 16px", borderRadius: "7px", border: "none", background: "var(--accent)", color: "white", fontSize: "12px", fontWeight: 600, cursor: "pointer", opacity: formName.trim() ? 1 : 0.5 }}>
                  <Save size={13} /> {editingTest ? "Update" : "Create"}
                </button>
                <button onClick={resetForm} style={{ padding: "8px 16px", borderRadius: "7px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", fontSize: "12px", cursor: "pointer" }}>Cancel</button>
              </div>
            </div>
          )}

          {/* Test List */}
          {loadingTests ? (
            <div style={{ textAlign: "center", padding: "40px", color: "var(--text2)" }}><Loader2 size={20} style={{ animation: "spin 1s linear infinite" }} /></div>
          ) : tests.length === 0 ? (
            <div style={{ textAlign: "center", padding: "60px 20px", color: "var(--text2)" }}>
              <FlaskConical size={36} style={{ marginBottom: "12px", opacity: 0.4 }} />
              <p style={{ fontSize: "14px" }}>No test cases yet.</p>
              <p style={{ fontSize: "12px" }}>Create one manually or ask Cilantro: "Create a unit test for patient age validation"</p>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              {tests.map((test) => (
                <div key={test.test_id} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "14px 16px", border: "1px solid var(--border)", borderRadius: "9px", background: "var(--surface)", transition: "border-color 0.12s" }}>
                  {/* Status indicator */}
                  <div style={{ width: "10px", height: "10px", borderRadius: "50%", background: test.last_result === "passed" ? "#22c55e" : test.last_result === "failed" ? "#ef4444" : "#94a3b8", flexShrink: 0 }} />
                  {/* Info */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text)" }}>{test.name}</div>
                    <div style={{ fontSize: "11px", color: "var(--text2)", marginTop: "2px" }}>{test.description || "No description"}</div>
                    <div style={{ display: "flex", gap: "5px", marginTop: "5px", flexWrap: "wrap" }}>
                      <span style={{ fontSize: "9px", padding: "2px 6px", borderRadius: "4px", background: "var(--surface2)", color: "var(--text2)", fontWeight: 600, textTransform: "uppercase" }}>{test.test_type}</span>
                      {test.tags?.map((tag) => <span key={tag} style={{ fontSize: "9px", padding: "2px 6px", borderRadius: "4px", background: "#dbeafe", color: "#2563eb" }}>{tag}</span>)}
                      {test.last_run_at && <span style={{ fontSize: "9px", color: "var(--text3)" }}>Last run: {new Date(test.last_run_at).toLocaleString()}</span>}
                    </div>
                  </div>
                  {/* Actions */}
                  <div style={{ display: "flex", gap: "4px", flexShrink: 0 }}>
                    <button onClick={() => runSingleTest(test.test_id)} disabled={runningTestId === test.test_id} title="Run test" style={{ width: "30px", height: "30px", borderRadius: "6px", border: "1px solid var(--border)", background: "none", color: "#22c55e", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      {runningTestId === test.test_id ? <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> : <Play size={13} />}
                    </button>
                    <button onClick={() => openEditForm(test)} title="Edit" style={{ width: "30px", height: "30px", borderRadius: "6px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <Edit3 size={13} />
                    </button>
                    <button onClick={() => deleteTest(test.test_id)} title="Delete" style={{ width: "30px", height: "30px", borderRadius: "6px", border: "1px solid var(--border)", background: "none", color: "#ef4444", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Traces Tab */}
      {activeTab === "traces" && (
        <TracesTab flowId={selectedFlowId} flowName={selectedFlow?.name || ""} />
      )}
    </div>
  );
}

// ─── Traces Tab Component ────────────────────────────────────────────────────

function TracesTab({ flowId, flowName }: { flowId: string; flowName: string }) {
  const [traces, setTraces] = useState<TraceRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [expandedTrace, setExpandedTrace] = useState<string | null>(null);
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [filterAction, setFilterAction] = useState<string>("all");

  const loadTraces = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch(`${API}/api/traces/${flowId}?limit=50`, { headers: API_KEY ? { "X-Api-Key": API_KEY } : {} });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const data = await res.json();
      setTraces(data.traces || []);
    } catch (e) {
      setTraces([]);
      console.error("Failed to load traces:", e);
    } finally {
      setLoading(false);
    }
  }, [flowId]);

  useEffect(() => {
    if (flowId) loadTraces(); // eslint-disable-line react-hooks/set-state-in-effect
  }, [flowId, loadTraces]);

  const loadTraceDetail = async (traceId: string) => {
    if (expandedTrace === traceId) { setExpandedTrace(null); setTraceDetail(null); return; }
    setExpandedTrace(traceId);
    try {
      const res = await fetch(`${API}/api/traces/detail/${traceId}`, { headers: API_KEY ? { "X-Api-Key": API_KEY } : {} });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const data = await res.json();
      setTraceDetail(data);
    } catch (e) {
      setTraceDetail(null);
      console.error("Failed to load trace detail:", e);
    }
  };

  const filtered = filterAction === "all" ? traces : traces.filter((t) => t.action === filterAction);
  const actionTypes = [...new Set(traces.map((t) => t.action))];

  const statusColor = (s: string) => s === "success" ? "#22c55e" : s === "failed" ? "#ef4444" : s === "running" ? "#3b82f6" : "#94a3b8";
  const actionColor = (a: string) => {
    const map: Record<string, string> = { "cilantro.chat": "#8b5cf6", "test.create": "#22c55e", "test.run": "#3b82f6", "test.delete": "#ef4444", "flow.compile": "#f59e0b", "flow.deploy": "#ec4899", "flow.execute": "#06b6d4", "pipeline.test": "#22c55e", "pipeline.scan": "#6366f1", "pipeline.deploy": "#f59e0b", "pipeline.run": "#3b82f6", "pipeline.activate": "#ec4899" };
    return map[a] || "#64748b";
  };

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <Activity size={16} style={{ color: "var(--accent)" }} />
          <span style={{ fontSize: "14px", fontWeight: 600 }}>Activity Traces</span>
          <span style={{ fontSize: "12px", color: "var(--text2)" }}>({filtered.length} events)</span>
          <Tooltip content="Observability & Tracing" steps={["Every action is traced with OpenTelemetry + X-Ray", "Click a trace to see the full waterfall breakdown", "Filter by action type to focus on specific operations", "Each span shows duration, status, and metadata"]} position="bottom" />
        </div>
        <div style={{ display: "flex", gap: "8px" }}>
          <select value={filterAction} onChange={(e) => setFilterAction(e.target.value)} style={{ padding: "6px 10px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface2)", color: "var(--text)", fontSize: "11px" }}>
            <option value="all">All actions</option>
            {actionTypes.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <button onClick={loadTraces} style={{ padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", fontSize: "11px", cursor: "pointer" }}>Refresh</button>
        </div>
      </div>

      {/* Service Map (mini) */}
      <div style={{ padding: "14px 16px", border: "1px solid var(--border)", borderRadius: "10px", background: "var(--surface)", marginBottom: "16px" }}>
        <div style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", marginBottom: "10px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Service Map — {flowName || "All Flows"}</div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap", justifyContent: "center" }}>
          {[
            { name: "FastAPI", color: "#22c55e" },
            { name: "Bedrock", color: "#8b5cf6" },
            { name: "DynamoDB", color: "#f59e0b" },
            { name: "HealthLake", color: "#3b82f6" },
            { name: "SQS", color: "#ec4899" },
            { name: "X-Ray", color: "#06b6d4" },
          ].map((svc, i) => (
            <div key={svc.name} style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <div style={{ width: "32px", height: "32px", borderRadius: "8px", background: `${svc.color}15`, border: `1px solid ${svc.color}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "10px", fontWeight: 700, color: svc.color }}>{svc.name.slice(0, 2)}</div>
              <span style={{ fontSize: "11px", color: "var(--text)" }}>{svc.name}</span>
              {i < 5 && <span style={{ color: "var(--text3)", fontSize: "14px" }}>→</span>}
            </div>
          ))}
        </div>
      </div>

      {/* Trace List */}
      {loading ? (
        <div style={{ textAlign: "center", padding: "40px", color: "var(--text2)" }}><Loader2 size={20} style={{ animation: "spin 1s linear infinite" }} /></div>
      ) : filtered.length === 0 ? (
        <div style={{ textAlign: "center", padding: "60px 20px", color: "var(--text2)" }}>
          <Activity size={36} style={{ marginBottom: "12px", opacity: 0.4 }} />
          <p style={{ fontSize: "14px" }}>No traces yet. Actions will appear here as they execute.</p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
          {filtered.map((trace) => (
            <div key={trace.trace_id} style={{ border: "1px solid var(--border)", borderRadius: "9px", background: "var(--surface)", overflow: "hidden" }}>
              <div onClick={() => loadTraceDetail(trace.trace_id)} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "12px 16px", cursor: "pointer", transition: "background 0.1s" }}>
                <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: statusColor(trace.status), flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--text)" }}>{trace.action}</span>
                    <span style={{ fontSize: "9px", padding: "2px 6px", borderRadius: "4px", background: `${actionColor(trace.action)}15`, color: actionColor(trace.action), fontWeight: 600 }}>{trace.service}</span>
                  </div>
                  <div style={{ fontSize: "11px", color: "var(--text2)", marginTop: "2px" }}>{trace.description}</div>
                </div>
                <div style={{ textAlign: "right", flexShrink: 0 }}>
                  <div style={{ fontSize: "11px", fontWeight: 600, color: "var(--text)" }}>{trace.duration_ms}ms</div>
                  <div style={{ fontSize: "9px", color: "var(--text3)" }}>{new Date(trace.timestamp).toLocaleTimeString()}</div>
                </div>
              </div>

              {/* Expanded waterfall */}
              {expandedTrace === trace.trace_id && traceDetail && (
                <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border)", background: "var(--surface2)" }}>
                  <div style={{ fontSize: "10px", fontWeight: 600, color: "var(--text2)", marginBottom: "8px", textTransform: "uppercase" }}>Trace Waterfall — {trace.trace_id.slice(0, 12)}</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                    {traceDetail.spans.map((span, i) => {
                      const pct = traceDetail.total_duration_ms > 0 ? (span.duration_ms / traceDetail.total_duration_ms) * 100 : 50;
                      const offset = traceDetail.total_duration_ms > 0 ? (span.offset_ms / traceDetail.total_duration_ms) * 100 : 0;
                      return (
                        <div key={i} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                          <span style={{ width: "100px", fontSize: "10px", color: "var(--text2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flexShrink: 0 }}>{span.name}</span>
                          <div style={{ flex: 1, height: "16px", background: "var(--surface)", borderRadius: "3px", position: "relative", overflow: "hidden" }}>
                            <div style={{ position: "absolute", left: `${offset}%`, width: `${Math.max(pct, 2)}%`, height: "100%", background: actionColor(span.service), borderRadius: "3px", opacity: 0.8 }} />
                          </div>
                          <span style={{ width: "45px", fontSize: "9px", color: "var(--text2)", textAlign: "right", flexShrink: 0 }}>{span.duration_ms}ms</span>
                        </div>
                      );
                    })}
                  </div>
                  {traceDetail.metadata && (
                    <div style={{ marginTop: "10px", padding: "8px 10px", background: "var(--surface)", borderRadius: "6px", fontSize: "10px", fontFamily: "monospace", color: "var(--text2)" }}>
                      {Object.entries(traceDetail.metadata).map(([k, v]) => (
                        <div key={k}><span style={{ color: "var(--accent)" }}>{k}:</span> {String(v)}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Trace Types ─────────────────────────────────────────────────────────────

interface TraceRecord {
  trace_id: string;
  flow_id: string;
  action: string;
  service: string;
  description: string;
  status: string;
  duration_ms: number;
  timestamp: string;
  user_id?: string;
}

interface TraceSpan {
  name: string;
  service: string;
  duration_ms: number;
  offset_ms: number;
  status: string;
}

interface TraceDetail {
  trace_id: string;
  spans: TraceSpan[];
  total_duration_ms: number;
  metadata: Record<string, unknown>;
}
