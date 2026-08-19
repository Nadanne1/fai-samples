/* eslint-disable react-refresh/only-export-components */
/**
 * Node Management Module
 *
 * Centralized node registry with:
 * - Built-in nodes from the screening agent
 * - Custom user-defined nodes with endpoint configuration
 * - Process-level feature enrichment (VoiceAI, Traceability, Observability)
 * - Import existing nodes into custom profiles
 */

import { useState, useCallback } from "react";
import { Plus, Save, Trash2, X, Settings, Mic, Eye, Activity, Globe, Edit3, Search } from "lucide-react";
import styles from "./NodeManager.module.css";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface NodeEndpoint {
  url: string;
  method: "GET" | "POST" | "PUT" | "DELETE";
  headers?: Record<string, string>;
  timeout?: number;
  retries?: number;
  authType?: "none" | "bearer" | "api-key" | "iam";
}

export interface NodeEnrichment {
  voiceAI: {
    enabled: boolean;
    provider?: "aws-polly" | "aws-transcribe" | "custom";
    language?: string;
    voiceId?: string;
  };
  traceability: {
    enabled: boolean;
    traceProvider?: "langsmith" | "xray" | "opentelemetry";
    projectName?: string;
    captureInputs?: boolean;
    captureOutputs?: boolean;
  };
  observability: {
    enabled: boolean;
    metricsProvider?: "cloudwatch" | "prometheus" | "datadog";
    logLevel?: "debug" | "info" | "warn" | "error";
    alertOnFailure?: boolean;
    latencyThresholdMs?: number;
  };
}

export interface ManagedNode {
  id: string;
  name: string;
  description: string;
  category: "data" | "process" | "decision" | "action" | "terminal";
  icon: string;
  isBuiltIn: boolean;
  endpoint?: NodeEndpoint;
  enrichment: NodeEnrichment;
  createdAt: string;
  updatedAt: string;
}

// ─── Default enrichment ──────────────────────────────────────────────────────

export function defaultEnrichment(): NodeEnrichment {
  return {
    voiceAI: { enabled: false },
    traceability: { enabled: false, captureInputs: true, captureOutputs: true },
    observability: { enabled: false, logLevel: "info", alertOnFailure: false, latencyThresholdMs: 5000 },
  };
}

// ─── Built-in nodes registry ─────────────────────────────────────────────────

export const BUILT_IN_NODES: ManagedNode[] = [
  { id: "bi-load_patient", name: "load_patient", description: "Load patient FHIR data from HealthLake", category: "data", icon: "📋", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-load_questionnaire", name: "load_questionnaire", description: "Load trial FHIR Questionnaire", category: "data", icon: "📝", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-ask_question", name: "ask_question", description: "Generate conversational question", category: "process", icon: "💬", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-validate_response", name: "validate_response", description: "Validate patient response", category: "process", icon: "✓", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-check_discrepancy", name: "check_discrepancy", description: "Compare with FHIR records", category: "process", icon: "🔍", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-check_enable_when", name: "check_enable_when", description: "Evaluate conditional logic", category: "process", icon: "⚡", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-check_completion", name: "check_completion", description: "Verify all items answered", category: "process", icon: "☑", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-resolve_eligibility", name: "resolve_eligibility", description: "Determine eligibility", category: "decision", icon: "⚖", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-deep_dive_module", name: "deep_dive_module", description: "Activate deep-dive questions", category: "decision", icon: "🔬", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-ask_clarification", name: "ask_clarification", description: "Request clarification", category: "action", icon: "❓", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-prompt_missing", name: "prompt_missing", description: "Prompt for missing items", category: "action", icon: "📌", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-record_declined", name: "record_declined", description: "Record declined items", category: "action", icon: "⊘", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-escalate", name: "escalate", description: "Escalate to PI review", category: "action", icon: "🚨", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
  { id: "bi-finalize_screening", name: "finalize_screening", description: "Persist results to HealthLake", category: "terminal", icon: "✅", isBuiltIn: true, enrichment: defaultEnrichment(), createdAt: "", updatedAt: "" },
];

// ─── Storage ─────────────────────────────────────────────────────────────────

const NODES_STORAGE_KEY = "flow-editor-managed-nodes";

export function loadManagedNodes(): ManagedNode[] {
  try {
    const raw = localStorage.getItem(NODES_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

export function saveManagedNodes(nodes: ManagedNode[]) {
  localStorage.setItem(NODES_STORAGE_KEY, JSON.stringify(nodes));
}

// ─── Node Manager Component ──────────────────────────────────────────────────

interface NodeManagerProps {
  open: boolean;
  onClose: () => void;
  customNodes: ManagedNode[];
  onNodesChange: (nodes: ManagedNode[]) => void;
  onAddToProfile: (node: ManagedNode) => void;
}

export default function NodeManager({ open, onClose, customNodes, onNodesChange, onAddToProfile }: NodeManagerProps) {
  const [activeTab, setActiveTab] = useState<"registry" | "create" | "enrichment">("registry");
  const [searchQuery, setSearchQuery] = useState("");
  const [editingNode, setEditingNode] = useState<ManagedNode | null>(null);
  const [filterCategory, setFilterCategory] = useState<string>("all");

  // Create/Edit form state
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formCategory, setFormCategory] = useState<ManagedNode["category"]>("process");
  const [formIcon, setFormIcon] = useState("⚙");
  const [formEndpointUrl, setFormEndpointUrl] = useState("");
  const [formEndpointMethod, setFormEndpointMethod] = useState<NodeEndpoint["method"]>("POST");
  const [formEndpointAuth, setFormEndpointAuth] = useState<NodeEndpoint["authType"]>("none");
  const [formEndpointTimeout, setFormEndpointTimeout] = useState(30);
  const [formEndpointRetries, setFormEndpointRetries] = useState(3);
  const [formVoiceEnabled, setFormVoiceEnabled] = useState(false);
  const [formVoiceProvider, setFormVoiceProvider] = useState<"aws-polly" | "aws-transcribe" | "custom">("aws-polly");
  const [formTraceEnabled, setFormTraceEnabled] = useState(false);
  const [formTraceProvider, setFormTraceProvider] = useState<"langsmith" | "xray" | "opentelemetry">("langsmith");
  const [formObsEnabled, setFormObsEnabled] = useState(false);
  const [formObsProvider, setFormObsProvider] = useState<"cloudwatch" | "prometheus" | "datadog">("cloudwatch");
  const [formObsLogLevel, setFormObsLogLevel] = useState<"debug" | "info" | "warn" | "error">("info");
  const [formObsAlertOnFailure, setFormObsAlertOnFailure] = useState(false);
  const [formObsLatency, setFormObsLatency] = useState(5000);

  const allNodes = [...BUILT_IN_NODES, ...customNodes];
  const filteredNodes = allNodes.filter((n) => {
    const matchesSearch = !searchQuery || n.name.toLowerCase().includes(searchQuery.toLowerCase()) || n.description.toLowerCase().includes(searchQuery.toLowerCase());
    const matchesCategory = filterCategory === "all" || n.category === filterCategory;
    return matchesSearch && matchesCategory;
  });

  const resetForm = () => {
    setFormName(""); setFormDesc(""); setFormCategory("process"); setFormIcon("⚙");
    setFormEndpointUrl(""); setFormEndpointMethod("POST"); setFormEndpointAuth("none");
    setFormEndpointTimeout(30); setFormEndpointRetries(3);
    setFormVoiceEnabled(false); setFormTraceEnabled(false); setFormObsEnabled(false);
    setFormObsAlertOnFailure(false); setFormObsLatency(5000); setFormObsLogLevel("info");
    setEditingNode(null);
  };

  const loadNodeIntoForm = (node: ManagedNode) => {
    setFormName(node.name); setFormDesc(node.description); setFormCategory(node.category); setFormIcon(node.icon);
    setFormEndpointUrl(node.endpoint?.url || ""); setFormEndpointMethod(node.endpoint?.method || "POST");
    setFormEndpointAuth(node.endpoint?.authType || "none");
    setFormEndpointTimeout(node.endpoint?.timeout || 30); setFormEndpointRetries(node.endpoint?.retries || 3);
    setFormVoiceEnabled(node.enrichment.voiceAI.enabled);
    setFormVoiceProvider(node.enrichment.voiceAI.provider || "aws-polly");
    setFormTraceEnabled(node.enrichment.traceability.enabled);
    setFormTraceProvider(node.enrichment.traceability.traceProvider || "langsmith");
    setFormObsEnabled(node.enrichment.observability.enabled);
    setFormObsProvider(node.enrichment.observability.metricsProvider || "cloudwatch");
    setFormObsLogLevel(node.enrichment.observability.logLevel || "info");
    setFormObsAlertOnFailure(node.enrichment.observability.alertOnFailure || false);
    setFormObsLatency(node.enrichment.observability.latencyThresholdMs || 5000);
    setEditingNode(node);
    setActiveTab("create");
  };

  const saveNode = useCallback(() => {
    if (!formName.trim()) return;
    const nodeName = formName.trim().replace(/\s+/g, "_").toLowerCase();
    const now = new Date().toISOString();

    const node: ManagedNode = {
      id: editingNode?.id || `custom-${Date.now()}`,
      name: nodeName,
      description: formDesc,
      category: formCategory,
      icon: formIcon,
      isBuiltIn: false,
      endpoint: formEndpointUrl ? {
        url: formEndpointUrl,
        method: formEndpointMethod,
        authType: formEndpointAuth,
        timeout: formEndpointTimeout,
        retries: formEndpointRetries,
      } : undefined,
      enrichment: {
        voiceAI: { enabled: formVoiceEnabled, provider: formVoiceProvider },
        traceability: { enabled: formTraceEnabled, traceProvider: formTraceProvider, captureInputs: true, captureOutputs: true },
        observability: { enabled: formObsEnabled, metricsProvider: formObsProvider, logLevel: formObsLogLevel, alertOnFailure: formObsAlertOnFailure, latencyThresholdMs: formObsLatency },
      },
      createdAt: editingNode?.createdAt || now,
      updatedAt: now,
    };

    if (editingNode) {
      onNodesChange(customNodes.map((n) => n.id === editingNode.id ? node : n));
    } else {
      onNodesChange([...customNodes, node]);
    }
    resetForm();
    setActiveTab("registry");
  }, [formName, formDesc, formCategory, formIcon, formEndpointUrl, formEndpointMethod, formEndpointAuth, formEndpointTimeout, formEndpointRetries, formVoiceEnabled, formVoiceProvider, formTraceEnabled, formTraceProvider, formObsEnabled, formObsProvider, formObsLogLevel, formObsAlertOnFailure, formObsLatency, editingNode, customNodes, onNodesChange]);

  const deleteNode = (nodeId: string) => {
    onNodesChange(customNodes.filter((n) => n.id !== nodeId));
  };

  if (!open) return null;

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.panel} onClick={(e) => e.stopPropagation()}>
        <div className={styles.header}>
          <h2 className={styles.title}>Node Management</h2>
          <button className={styles.closeBtn} onClick={onClose}><X size={18} /></button>
        </div>

        {/* Tabs */}
        <div className={styles.tabs}>
          <button className={`${styles.tab} ${activeTab === "registry" ? styles.tabActive : ""}`} onClick={() => setActiveTab("registry")}>
            <Settings size={13} /> Registry
          </button>
          <button className={`${styles.tab} ${activeTab === "create" ? styles.tabActive : ""}`} onClick={() => { resetForm(); setActiveTab("create"); }}>
            <Plus size={13} /> {editingNode ? "Edit Node" : "Create Node"}
          </button>
          <button className={`${styles.tab} ${activeTab === "enrichment" ? styles.tabActive : ""}`} onClick={() => setActiveTab("enrichment")}>
            <Activity size={13} /> Enrichment
          </button>
        </div>

        {/* Registry Tab */}
        {activeTab === "registry" && (
          <div className={styles.body}>
            <div className={styles.searchRow}>
              <div className={styles.searchWrap}>
                <Search size={14} className={styles.searchIcon} />
                <input className={styles.searchInput} placeholder="Search nodes..." value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} />
              </div>
              <select className={styles.filterSelect} value={filterCategory} onChange={(e) => setFilterCategory(e.target.value)}>
                <option value="all">All Categories</option>
                <option value="data">Data</option>
                <option value="process">Process</option>
                <option value="decision">Decision</option>
                <option value="action">Action</option>
                <option value="terminal">Terminal</option>
              </select>
            </div>

            <div className={styles.nodeList}>
              {filteredNodes.map((node) => (
                <div key={node.id} className={styles.nodeCard}>
                  <div className={styles.nodeCardIcon}>{node.icon}</div>
                  <div className={styles.nodeCardInfo}>
                    <div className={styles.nodeCardName}>{node.name.replace(/_/g, " ")}</div>
                    <div className={styles.nodeCardDesc}>{node.description}</div>
                    <div className={styles.nodeCardMeta}>
                      <span className={styles.categoryBadge} data-cat={node.category}>{node.category}</span>
                      {node.isBuiltIn && <span className={styles.builtInBadge}>built-in</span>}
                      {node.endpoint && <span className={styles.endpointBadge}><Globe size={9} /> endpoint</span>}
                      {node.enrichment.voiceAI.enabled && <span className={styles.enrichBadge}><Mic size={9} /></span>}
                      {node.enrichment.traceability.enabled && <span className={styles.enrichBadge}><Eye size={9} /></span>}
                      {node.enrichment.observability.enabled && <span className={styles.enrichBadge}><Activity size={9} /></span>}
                    </div>
                  </div>
                  <div className={styles.nodeCardActions}>
                    <button className={styles.nodeActionBtn} onClick={() => onAddToProfile(node)} title="Add to active profile"><Plus size={13} /></button>
                    {!node.isBuiltIn && <button className={styles.nodeActionBtn} onClick={() => loadNodeIntoForm(node)} title="Edit"><Edit3 size={13} /></button>}
                    {!node.isBuiltIn && <button className={`${styles.nodeActionBtn} ${styles.nodeActionDanger}`} onClick={() => deleteNode(node.id)} title="Delete"><Trash2 size={13} /></button>}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Create/Edit Tab */}
        {activeTab === "create" && (
          <div className={styles.body}>
            <div className={styles.formSection}>
              <div className={styles.formSectionTitle}>Basic Info</div>
              <div className={styles.formGrid}>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Name</label>
                  <input className={styles.formInput} value={formName} onChange={(e) => setFormName(e.target.value)} placeholder="e.g. fetch_lab_results" />
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Category</label>
                  <select className={styles.formSelect} value={formCategory} onChange={(e) => setFormCategory(e.target.value as ManagedNode["category"])}>
                    <option value="data">Data Loading</option>
                    <option value="process">Processing</option>
                    <option value="decision">Decision</option>
                    <option value="action">Action</option>
                    <option value="terminal">Terminal</option>
                  </select>
                </div>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Description</label>
                <input className={styles.formInput} value={formDesc} onChange={(e) => setFormDesc(e.target.value)} placeholder="What does this node do?" />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Icon</label>
                <div className={styles.iconGrid}>
                  {["📋","📝","💬","✓","🔍","⚡","☑","⚖","🔬","❓","📌","⊘","🚨","✅","⚙","🧠","📊","🔗","🛡","📡","🗄","💾","🔄","⏱","🎯","📦","🧪","🔧"].map((ic) => (
                    <button key={ic} className={`${styles.iconBtn} ${formIcon === ic ? styles.iconBtnActive : ""}`} onClick={() => setFormIcon(ic)}>{ic}</button>
                  ))}
                </div>
              </div>
            </div>

            <div className={styles.formSection}>
              <div className={styles.formSectionTitle}><Globe size={13} /> Endpoint Configuration</div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>URL</label>
                <input className={styles.formInput} value={formEndpointUrl} onChange={(e) => setFormEndpointUrl(e.target.value)} placeholder="https://api.example.com/v1/process" />
              </div>
              <div className={styles.formGrid}>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Method</label>
                  <select className={styles.formSelect} value={formEndpointMethod} onChange={(e) => setFormEndpointMethod(e.target.value as NodeEndpoint["method"])}>
                    <option value="GET">GET</option>
                    <option value="POST">POST</option>
                    <option value="PUT">PUT</option>
                    <option value="DELETE">DELETE</option>
                  </select>
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Auth</label>
                  <select className={styles.formSelect} value={formEndpointAuth} onChange={(e) => setFormEndpointAuth(e.target.value as NodeEndpoint["authType"])}>
                    <option value="none">None</option>
                    <option value="bearer">Bearer Token</option>
                    <option value="api-key">API Key</option>
                    <option value="iam">IAM SigV4</option>
                  </select>
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Timeout (s)</label>
                  <input className={styles.formInput} type="number" value={formEndpointTimeout} onChange={(e) => setFormEndpointTimeout(Number(e.target.value))} />
                </div>
                <div className={styles.formGroup}>
                  <label className={styles.formLabel}>Retries</label>
                  <input className={styles.formInput} type="number" value={formEndpointRetries} onChange={(e) => setFormEndpointRetries(Number(e.target.value))} />
                </div>
              </div>
            </div>

            <div className={styles.formSection}>
              <div className={styles.formSectionTitle}><Activity size={13} /> Process Enrichment</div>

              {/* VoiceAI */}
              <div className={styles.enrichRow}>
                <label className={styles.enrichToggle}>
                  <input type="checkbox" checked={formVoiceEnabled} onChange={(e) => setFormVoiceEnabled(e.target.checked)} />
                  <Mic size={13} /> VoiceAI
                </label>
                {formVoiceEnabled && (
                  <select className={styles.enrichSelect} value={formVoiceProvider} onChange={(e) => setFormVoiceProvider(e.target.value as typeof formVoiceProvider)}>
                    <option value="aws-polly">AWS Polly (TTS)</option>
                    <option value="aws-transcribe">AWS Transcribe (STT)</option>
                    <option value="custom">Custom Provider</option>
                  </select>
                )}
              </div>

              {/* Traceability */}
              <div className={styles.enrichRow}>
                <label className={styles.enrichToggle}>
                  <input type="checkbox" checked={formTraceEnabled} onChange={(e) => setFormTraceEnabled(e.target.checked)} />
                  <Eye size={13} /> Traceability
                </label>
                {formTraceEnabled && (
                  <select className={styles.enrichSelect} value={formTraceProvider} onChange={(e) => setFormTraceProvider(e.target.value as typeof formTraceProvider)}>
                    <option value="langsmith">LangSmith</option>
                    <option value="xray">AWS X-Ray</option>
                    <option value="opentelemetry">OpenTelemetry</option>
                  </select>
                )}
              </div>

              {/* Observability */}
              <div className={styles.enrichRow}>
                <label className={styles.enrichToggle}>
                  <input type="checkbox" checked={formObsEnabled} onChange={(e) => setFormObsEnabled(e.target.checked)} />
                  <Activity size={13} /> Observability
                </label>
                {formObsEnabled && (
                  <div className={styles.enrichDetails}>
                    <select className={styles.enrichSelect} value={formObsProvider} onChange={(e) => setFormObsProvider(e.target.value as typeof formObsProvider)}>
                      <option value="cloudwatch">CloudWatch</option>
                      <option value="prometheus">Prometheus</option>
                      <option value="datadog">Datadog</option>
                    </select>
                    <select className={styles.enrichSelect} value={formObsLogLevel} onChange={(e) => setFormObsLogLevel(e.target.value as typeof formObsLogLevel)}>
                      <option value="debug">Debug</option>
                      <option value="info">Info</option>
                      <option value="warn">Warn</option>
                      <option value="error">Error</option>
                    </select>
                    <label className={styles.enrichCheckbox}>
                      <input type="checkbox" checked={formObsAlertOnFailure} onChange={(e) => setFormObsAlertOnFailure(e.target.checked)} />
                      Alert on failure
                    </label>
                  </div>
                )}
              </div>
            </div>

            <div className={styles.formActions}>
              <button className={styles.saveBtn} onClick={saveNode} disabled={!formName.trim()}>
                <Save size={13} /> {editingNode ? "Update Node" : "Create Node"}
              </button>
              {editingNode && <button className={styles.cancelBtn} onClick={resetForm}>Cancel Edit</button>}
            </div>
          </div>
        )}

        {/* Enrichment Overview Tab */}
        {activeTab === "enrichment" && (
          <div className={styles.body}>
            <div className={styles.enrichOverview}>
              <div className={styles.enrichCard}>
                <div className={styles.enrichCardHeader}><Mic size={16} /> VoiceAI</div>
                <div className={styles.enrichCardDesc}>Enable voice input/output for patient-facing nodes. Supports AWS Polly for text-to-speech and AWS Transcribe for speech-to-text.</div>
                <div className={styles.enrichCardNodes}>
                  <span className={styles.enrichCardLabel}>Enabled on:</span>
                  {allNodes.filter((n) => n.enrichment.voiceAI.enabled).map((n) => (
                    <span key={n.id} className={styles.enrichNodeChip}>{n.icon} {n.name.replace(/_/g, " ")}</span>
                  ))}
                  {allNodes.filter((n) => n.enrichment.voiceAI.enabled).length === 0 && <span className={styles.enrichNone}>No nodes configured</span>}
                </div>
              </div>

              <div className={styles.enrichCard}>
                <div className={styles.enrichCardHeader}><Eye size={16} /> Traceability</div>
                <div className={styles.enrichCardDesc}>Capture execution traces for debugging and audit. Supports LangSmith, AWS X-Ray, and OpenTelemetry.</div>
                <div className={styles.enrichCardNodes}>
                  <span className={styles.enrichCardLabel}>Enabled on:</span>
                  {allNodes.filter((n) => n.enrichment.traceability.enabled).map((n) => (
                    <span key={n.id} className={styles.enrichNodeChip}>{n.icon} {n.name.replace(/_/g, " ")}</span>
                  ))}
                  {allNodes.filter((n) => n.enrichment.traceability.enabled).length === 0 && <span className={styles.enrichNone}>No nodes configured</span>}
                </div>
              </div>

              <div className={styles.enrichCard}>
                <div className={styles.enrichCardHeader}><Activity size={16} /> Observability</div>
                <div className={styles.enrichCardDesc}>Monitor node performance with metrics, logging, and alerting. Supports CloudWatch, Prometheus, and Datadog.</div>
                <div className={styles.enrichCardNodes}>
                  <span className={styles.enrichCardLabel}>Enabled on:</span>
                  {allNodes.filter((n) => n.enrichment.observability.enabled).map((n) => (
                    <span key={n.id} className={styles.enrichNodeChip}>{n.icon} {n.name.replace(/_/g, " ")}</span>
                  ))}
                  {allNodes.filter((n) => n.enrichment.observability.enabled).length === 0 && <span className={styles.enrichNone}>No nodes configured</span>}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
