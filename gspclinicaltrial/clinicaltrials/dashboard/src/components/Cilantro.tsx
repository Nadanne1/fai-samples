/* eslint-disable react-refresh/only-export-components */
/**
 * Cilantro — Abstract Chat Agent Module
 *
 * A screen-aware AI assistant powered by Amazon Bedrock (default: Amazon Nova).
 * Runs on AgentCore as a chat agent with per-screen contextualization,
 * configurable guardrails, and reusable system prompts.
 * Chat history stored in DynamoDB. Real Bedrock integration (no mocks).
 */

import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { Send, X, Settings, Loader2, Minimize2, Maximize2, Bot, Sparkles, Copy, Check, Palette, Trash2 } from "lucide-react";
import Tooltip from "./Tooltip";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface CilantroMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  model?: string;
}

export interface CilantroConfig {
  screenId: string;
  screenName: string;
  purpose: string;
  systemPrompt: string;
  guardrailId?: string;
  guardrailVersion?: string;
  suggestedPrompts: string[];
}

interface CilantroModel {
  id: string;
  name: string;
  provider: string;
}

// ─── Color Themes ────────────────────────────────────────────────────────────

interface ColorTheme {
  id: string;
  name: string;
  gradient: string;
  accent: string;
  userBubble: string;
}

const COLOR_THEMES: ColorTheme[] = [
  { id: "purple", name: "Purple (Default)", gradient: "linear-gradient(135deg, #6366f1, #8b5cf6)", accent: "#6366f1", userBubble: "#6366f1" },
  { id: "blue", name: "Ocean Blue", gradient: "linear-gradient(135deg, #3b82f6, #06b6d4)", accent: "#3b82f6", userBubble: "#3b82f6" },
  { id: "green", name: "Forest Green", gradient: "linear-gradient(135deg, #16a34a, #059669)", accent: "#16a34a", userBubble: "#16a34a" },
  { id: "orange", name: "Sunset Orange", gradient: "linear-gradient(135deg, #f59e0b, #ef4444)", accent: "#f59e0b", userBubble: "#ea580c" },
  { id: "pink", name: "Rose Pink", gradient: "linear-gradient(135deg, #ec4899, #f43f5e)", accent: "#ec4899", userBubble: "#ec4899" },
  { id: "dark", name: "Midnight", gradient: "linear-gradient(135deg, #1e293b, #334155)", accent: "#64748b", userBubble: "#475569" },
  { id: "teal", name: "Teal", gradient: "linear-gradient(135deg, #0d9488, #06b6d4)", accent: "#0d9488", userBubble: "#0d9488" },
];

const THEME_STORAGE_KEY = "cilantro-color-theme";

function loadThemeId(): string {
  return localStorage.getItem(THEME_STORAGE_KEY) || "purple";
}

function saveThemeId(id: string) {
  localStorage.setItem(THEME_STORAGE_KEY, id);
}

// ─── Available Models ────────────────────────────────────────────────────────

const MODELS: CilantroModel[] = [
  { id: "amazon.nova-pro-v1:0", name: "Amazon Nova Pro", provider: "Amazon" },
  { id: "amazon.nova-lite-v1:0", name: "Amazon Nova Lite", provider: "Amazon" },
  { id: "amazon.nova-micro-v1:0", name: "Amazon Nova Micro", provider: "Amazon" },
  { id: "us.anthropic.claude-3-5-sonnet-20241022-v2:0", name: "Claude 3.5 Sonnet", provider: "Anthropic" },
  { id: "us.anthropic.claude-3-5-haiku-20241022-v1:0", name: "Claude 3.5 Haiku", provider: "Anthropic" },
  { id: "us.meta.llama3-2-90b-instruct-v1:0", name: "Llama 3.2 90B", provider: "Meta" },
  { id: "mistral.mistral-large-2407-v1:0", name: "Mistral Large", provider: "Mistral" },
];

// ─── Screen Configurations ───────────────────────────────────────────────────

export const SCREEN_CONFIGS: Record<string, CilantroConfig> = {
  screening: { screenId: "screening", screenName: "Patient Screening", purpose: "Help explain patient eligibility, clinical evidence, and trial matching decisions.", systemPrompt: "You are Cilantro, an assistant specialized in clinical trial patient screening. Help users understand eligibility criteria, interpret patient record evidence, troubleshoot screening issues, and optimize the screening workflow.", guardrailId: import.meta.env.VITE_GUARDRAIL_ID || '', guardrailVersion: "DRAFT", suggestedPrompts: ["Why was this patient marked ineligible?", "Explain the evidence behind this result", "What criteria need follow-up?", "Summarize this patient's screening history"] },
  analytics: { screenId: "analytics", screenName: "Analytics", purpose: "Help build charts, interpret screening data, identify trends, and generate insights.", systemPrompt: "You are Cilantro, an AI assistant for clinical trial analytics. Help users create visualizations, interpret eligibility trends, identify common failure criteria, and generate data-driven insights.", suggestedPrompts: ["What are the top reasons for ineligibility?", "Create a chart showing weekly screening volume", "Compare eligibility rates across trials", "Identify trends in borderline cases"] },
  builder: { screenId: "builder", screenName: "Trial Builder", purpose: "Help design trial protocols, write eligibility criteria, and prepare studies for screening.", systemPrompt: "You are Cilantro, an assistant for clinical trial design. Help users write inclusion/exclusion criteria, structure screening questions, and configure follow-up modules.", suggestedPrompts: ["Write inclusion criteria for a diabetes trial", "Add a cardiovascular follow-up module", "What criteria need clarification?", "Import NCT04000001 and customize it"] },
  "flow-editor": { screenId: "flow-editor", screenName: "Flow Editor", purpose: "Help model recruitment workflows, configure steps, and refine decision routing.", systemPrompt: "You are Cilantro, an assistant for recruitment workflow design. Help users model workflow steps, write routing logic, configure node behavior, and explain implementation details when asked.", suggestedPrompts: ["Add a retry loop after validation failure", "Write routing logic for eligibility", "How should I structure candidate follow-up?", "Show the implementation for this flow"] },
  devops: { screenId: "devops", screenName: "Dev Ops", purpose: "Help build test cases, validate workflows, review activity traces, and keep the demo environment ready.", systemPrompt: "You are Cilantro, an assistant for demo operations and workflow validation. You can CREATE, READ, UPDATE, and DELETE test cases for flows.\n\nWhen the user asks you to create, edit, list, or delete a test, respond with your explanation AND include a JSON action block that will be executed automatically.\n\nAction format (wrap in ```json code block):\n- Create: {\"action\": \"create_test\", \"params\": {\"flow_id\": \"<id>\", \"name\": \"<name>\", \"description\": \"<desc>\", \"test_type\": \"unit|integration|e2e|load\", \"input_state\": {}, \"expected_output\": {}, \"tags\": []}}\n- List: {\"action\": \"list_tests\", \"params\": {\"flow_id\": \"<id>\"}}\n- Update: {\"action\": \"update_test\", \"params\": {\"flow_id\": \"<id>\", \"test_id\": \"<id>\", \"name\": \"<new_name>\", ...}}\n- Delete: {\"action\": \"delete_test\", \"params\": {\"flow_id\": \"<id>\", \"test_id\": \"<id>\"}}\n- Run: {\"action\": \"run_test\", \"params\": {\"flow_id\": \"<id>\", \"test_id\": \"<id>\"}}\n\nThe current flow_id context will be provided. Always include the action block when performing CRUD operations. Be helpful and suggest good test names, descriptions, and input states.", suggestedPrompts: ["Create a unit test for patient age validation", "List all tests for this flow", "Create an integration test for the full screening", "Write a load test with 100 concurrent patients"] },
  users: { screenId: "users", screenName: "Admin", purpose: "Help manage user access, role groups, demo reset, and access troubleshooting.", systemPrompt: "You are Cilantro, an assistant for access management. Help users create groups, assign permissions, configure role-based access, and prepare or reset the demo environment.", suggestedPrompts: ["Set up groups for a multi-site trial", "What permissions does a coordinator need?", "How do I restrict workflow access by role?", "How should I reset the demo?"] },
};

// ─── Storage ─────────────────────────────────────────────────────────────────

const CONFIG_KEY = "cilantro-custom-configs";

function loadCustomConfigs(): Record<string, Partial<CilantroConfig>> {
  try { return JSON.parse(localStorage.getItem(CONFIG_KEY) || "{}"); } catch { return {}; }
}

function saveCustomConfigs(configs: Record<string, Partial<CilantroConfig>>) {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(configs));
}

// ─── API calls ───────────────────────────────────────────────────────────────

const API = import.meta.env.VITE_API_URL || "";
const API_KEY = import.meta.env.VITE_API_KEY || "";
const authHeaders: Record<string, string> = API_KEY ? { "X-Api-Key": API_KEY } : {};

async function sendCilantroMessage(params: {
  sessionId: string; screenId: string; message: string; modelId: string;
  systemPrompt: string; guardrailId?: string; guardrailVersion?: string;
  history: { role: string; content: string }[];
}): Promise<{ response: string; model_id: string; usage?: Record<string, number>; error?: string }> {
  const res = await fetch(`${API}/api/cilantro/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({
      session_id: params.sessionId,
      screen_id: params.screenId,
      message: params.message,
      model_id: params.modelId,
      system_prompt: params.systemPrompt,
      guardrail_id: params.guardrailId || null,
      guardrail_version: params.guardrailVersion || null,
      history: params.history,
    }),
  });
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  return res.json();
}

async function fetchCilantroHistory(sessionId: string): Promise<CilantroMessage[]> {
  try {
    const res = await fetch(`${API}/api/cilantro/history/${sessionId}`, { headers: { ...authHeaders } });
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    const data = await res.json();
    return (data.messages || []).map((m: Record<string, string>) => ({
      id: `${m.session_id}-${m.timestamp}`,
      role: m.role,
      content: m.content,
      timestamp: m.timestamp,
      model: m.model_id,
    }));
  } catch { return []; }
}

async function clearCilantroHistory(sessionId: string): Promise<void> {
  const res = await fetch(`${API}/api/cilantro/history/${sessionId}`, { method: "DELETE", headers: { ...authHeaders } });
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
}

// ─── Main Component ──────────────────────────────────────────────────────────

interface CilantroProps {
  screenId: string;
}

export default function Cilantro({ screenId }: CilantroProps) {
  const config = useMemo<CilantroConfig>(() => {
    const baseConfig = SCREEN_CONFIGS[screenId] || SCREEN_CONFIGS["screening"];
    const customConfigs = loadCustomConfigs();
    const customOverrides = customConfigs[screenId] || {};
    return { ...baseConfig, ...customOverrides };
  }, [screenId]);

  const [open, setOpen] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [messages, setMessages] = useState<CilantroMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState(MODELS[0].id);
  const [showSettings, setShowSettings] = useState(false);
  const [showThemePicker, setShowThemePicker] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copiedAll, setCopiedAll] = useState(false);

  // Theme
  const [themeId, setThemeId] = useState(loadThemeId);
  const theme = COLOR_THEMES.find((t) => t.id === themeId) || COLOR_THEMES[0];

  // Settings form
  const [editSystemPrompt, setEditSystemPrompt] = useState(config.systemPrompt);
  const [editGuardrailId, setEditGuardrailId] = useState(config.guardrailId || "");
  const [editGuardrailVersion, setEditGuardrailVersion] = useState(config.guardrailVersion || "");

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionId = useRef(`cilantro-${screenId}-${Date.now()}`);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // Load history from DynamoDB on screen change
  useEffect(() => {
    sessionId.current = `cilantro-${screenId}-session`;
    fetchCilantroHistory(sessionId.current).then(setMessages);
    const cfg = { ...SCREEN_CONFIGS[screenId], ...(loadCustomConfigs()[screenId] || {}) };
    setEditSystemPrompt(cfg.systemPrompt || "");
    setEditGuardrailId(cfg.guardrailId || "");
    setEditGuardrailVersion(cfg.guardrailVersion || "");
  }, [screenId]);

  // ─── Send message (real Bedrock) ───────────────────────────────────────────

  const sendMessage = useCallback(async (text?: string) => {
    const msg = (text || input).trim();
    if (!msg || loading) return;

    const userMsg: CilantroMessage = { id: `msg-${Date.now()}`, role: "user", content: msg, timestamp: new Date().toISOString() };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const history = messages.map((m) => ({ role: m.role, content: m.content }));
      const result = await sendCilantroMessage({
        sessionId: sessionId.current,
        screenId: config.screenId,
        message: msg,
        modelId: selectedModel,
        systemPrompt: editSystemPrompt || config.systemPrompt,
        guardrailId: editGuardrailId || config.guardrailId,
        guardrailVersion: editGuardrailVersion || config.guardrailVersion,
        history,
      });

      const model = MODELS.find((m) => m.id === selectedModel) || MODELS[0];
      const assistantMsg: CilantroMessage = {
        id: `msg-${Date.now() + 1}`,
        role: "assistant",
        content: result.response,
        timestamp: new Date().toISOString(),
        model: model.name,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      setMessages((prev) => [...prev, { id: `msg-${Date.now() + 1}`, role: "system", content: `Connection error: ${err instanceof Error ? err.message : 'Please try again.'}`, timestamp: new Date().toISOString() }]);
    } finally {
      setLoading(false);
    }
  }, [input, loading, selectedModel, messages, config, editSystemPrompt, editGuardrailId, editGuardrailVersion]);

  // ─── Copy functions ────────────────────────────────────────────────────────

  const copyMessage = (msg: CilantroMessage) => {
    navigator.clipboard.writeText(msg.content);
    setCopiedId(msg.id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const copyAllHistory = () => {
    const text = messages.map((m) => `[${m.role.toUpperCase()}] ${m.content}`).join("\n\n");
    navigator.clipboard.writeText(text);
    setCopiedAll(true);
    setTimeout(() => setCopiedAll(false), 2000);
  };

  // ─── Settings ──────────────────────────────────────────────────────────────

  const clearHistory = async () => {
    await clearCilantroHistory(sessionId.current);
    setMessages([]);
  };

  const saveSettings = () => {
    const configs = loadCustomConfigs();
    configs[screenId] = { systemPrompt: editSystemPrompt, guardrailId: editGuardrailId || undefined, guardrailVersion: editGuardrailVersion || undefined };
    saveCustomConfigs(configs);
    setShowSettings(false);
  };

  const selectTheme = (id: string) => {
    setThemeId(id);
    saveThemeId(id);
    setShowThemePicker(false);
  };

  const currentModel = MODELS.find((m) => m.id === selectedModel) || MODELS[0];

  // ─── Closed state ──────────────────────────────────────────────────────────

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} style={{
        position: "fixed", bottom: "24px", left: "24px", width: "56px", height: "56px",
        borderRadius: "50%", background: theme.gradient, border: "none", color: "white",
        cursor: "pointer", boxShadow: `0 4px 20px ${theme.accent}66`,
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9998,
        transition: "transform 0.2s",
      }} onMouseEnter={(e) => { e.currentTarget.style.transform = "scale(1.1)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.transform = "scale(1)"; }}
        title={`Cilantro — ${config.screenName} Assistant`}
      >
        <Bot size={24} />
      </button>
    );
  }

  // ─── Open state ────────────────────────────────────────────────────────────

  return (
    <div style={{
      position: "fixed", bottom: "24px", left: "24px",
      width: minimized ? "300px" : "420px", height: minimized ? "48px" : "620px",
      borderRadius: "14px", background: "var(--surface)", border: "1px solid var(--border)",
      boxShadow: "0 12px 40px rgba(0,0,0,0.15)", zIndex: 9998,
      display: "flex", flexDirection: "column", overflow: "hidden", transition: "width 0.2s, height 0.2s",
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: "8px", padding: "12px 16px", background: theme.gradient, color: "white", flexShrink: 0, cursor: "pointer" }} onClick={() => setMinimized(!minimized)}>
        <Bot size={18} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: "13px", fontWeight: 700 }}>Cilantro</div>
          <div style={{ fontSize: "10px", opacity: 0.8 }}>{config.screenName} &middot; {currentModel.name}</div>
        </div>
        {/* Copy all */}
        {messages.length > 0 && !minimized && (
          <button onClick={(e) => { e.stopPropagation(); copyAllHistory(); }} style={{ background: "rgba(255,255,255,0.2)", border: "none", borderRadius: "6px", padding: "4px", color: "white", cursor: "pointer" }} title="Copy entire chat">
            {copiedAll ? <Check size={14} /> : <Copy size={14} />}
          </button>
        )}
        <button onClick={(e) => { e.stopPropagation(); setShowThemePicker(!showThemePicker); }} style={{ background: "rgba(255,255,255,0.2)", border: "none", borderRadius: "6px", padding: "4px", color: "white", cursor: "pointer" }} title="Color theme"><Palette size={14} /></button>
        <button onClick={(e) => { e.stopPropagation(); setShowSettings(!showSettings); }} style={{ background: "rgba(255,255,255,0.2)", border: "none", borderRadius: "6px", padding: "4px", color: "white", cursor: "pointer" }}><Settings size={14} /></button>
        <button onClick={(e) => { e.stopPropagation(); setMinimized(!minimized); }} style={{ background: "rgba(255,255,255,0.2)", border: "none", borderRadius: "6px", padding: "4px", color: "white", cursor: "pointer" }}>{minimized ? <Maximize2 size={14} /> : <Minimize2 size={14} />}</button>
        <button onClick={(e) => { e.stopPropagation(); setOpen(false); }} style={{ background: "rgba(255,255,255,0.2)", border: "none", borderRadius: "6px", padding: "4px", color: "white", cursor: "pointer" }}><X size={14} /></button>
      </div>

      {!minimized && (<>
        {/* Theme Picker */}
        {showThemePicker && (
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)", background: "var(--surface2)", display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" }}>
            <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--text2)", marginRight: "4px" }}>Theme:</span>
            {COLOR_THEMES.map((t) => (
              <button key={t.id} onClick={() => selectTheme(t.id)} title={t.name} style={{
                width: "28px", height: "28px", borderRadius: "50%", background: t.gradient, border: themeId === t.id ? "3px solid var(--text)" : "2px solid transparent",
                cursor: "pointer", transition: "border 0.12s",
              }} />
            ))}
          </div>
        )}

        {/* Settings Panel */}
        {showSettings && (
          <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)", background: "var(--surface2)", fontSize: "12px", display: "flex", flexDirection: "column", gap: "10px", maxHeight: "260px", overflow: "auto" }}>
            <div style={{ fontWeight: 700, fontSize: "13px", display: "flex", alignItems: "center", gap: "6px" }}>
              <Settings size={13} /> Configuration
              <Tooltip content="Cilantro Settings" steps={["Select a Bedrock model", "Customize the system prompt", "Add a Guardrail ID for content filtering", "Settings persist per-screen"]} position="left" />
            </div>
            <div>
              <div style={{ fontWeight: 600, color: "var(--text2)", marginBottom: "4px" }}>Model</div>
              <select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)} style={{ width: "100%", padding: "7px 10px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", fontSize: "12px" }}>
                {MODELS.map((m) => <option key={m.id} value={m.id}>{m.name} ({m.provider})</option>)}
              </select>
            </div>
            <div>
              <div style={{ fontWeight: 600, color: "var(--text2)", marginBottom: "4px" }}>System Prompt</div>
              <textarea value={editSystemPrompt} onChange={(e) => setEditSystemPrompt(e.target.value)} style={{ width: "100%", padding: "8px 10px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", fontSize: "11px", fontFamily: "monospace", minHeight: "60px", resize: "vertical", outline: "none" }} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
              <div>
                <div style={{ fontWeight: 600, color: "var(--text2)", marginBottom: "4px" }}>Guardrail ID</div>
                <input value={editGuardrailId} onChange={(e) => setEditGuardrailId(e.target.value)} placeholder="e.g. hkz0mskh1qrs" style={{ width: "100%", padding: "7px 10px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", fontSize: "12px", outline: "none" }} />
              </div>
              <div>
                <div style={{ fontWeight: 600, color: "var(--text2)", marginBottom: "4px" }}>Version</div>
                <input value={editGuardrailVersion} onChange={(e) => setEditGuardrailVersion(e.target.value)} placeholder="DRAFT" style={{ width: "100%", padding: "7px 10px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", fontSize: "12px", outline: "none" }} />
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px" }}>
              <button onClick={saveSettings} style={{ padding: "7px 14px", borderRadius: "6px", border: "none", background: theme.accent, color: "white", fontSize: "11px", fontWeight: 600, cursor: "pointer" }}>Save</button>
              <button onClick={() => setShowSettings(false)} style={{ padding: "7px 14px", borderRadius: "6px", border: "1px solid var(--border)", background: "none", color: "var(--text2)", fontSize: "11px", cursor: "pointer" }}>Close</button>
              <button onClick={clearHistory} style={{ marginLeft: "auto", padding: "7px 14px", borderRadius: "6px", border: "1px solid #ef4444", background: "none", color: "#ef4444", fontSize: "11px", cursor: "pointer", display: "flex", alignItems: "center", gap: "4px" }}><Trash2 size={11} /> Clear</button>
            </div>
          </div>
        )}

        {/* Messages */}
        <div style={{ flex: 1, overflow: "auto", padding: "12px 16px", display: "flex", flexDirection: "column", gap: "10px" }}>
          {messages.length === 0 && (
            <div style={{ textAlign: "center", padding: "30px 10px", color: "var(--text2)" }}>
              <Sparkles size={28} style={{ marginBottom: "8px", color: theme.accent }} />
              <div style={{ fontSize: "14px", fontWeight: 600, color: "var(--text)", marginBottom: "4px" }}>Cilantro — {config.screenName}</div>
              <div style={{ fontSize: "12px", lineHeight: "1.5" }}>{config.purpose}</div>
              <div style={{ marginTop: "16px", display: "flex", flexDirection: "column", gap: "6px" }}>
                {config.suggestedPrompts.map((p, i) => (
                  <button key={i} onClick={() => sendMessage(p)} style={{ padding: "8px 12px", borderRadius: "8px", border: "1px solid var(--border)", background: "var(--surface2)", color: "var(--text)", fontSize: "11px", cursor: "pointer", textAlign: "left", transition: "border-color 0.12s" }}
                    onMouseEnter={(e) => { (e.target as HTMLElement).style.borderColor = theme.accent; }}
                    onMouseLeave={(e) => { (e.target as HTMLElement).style.borderColor = "var(--border)"; }}
                  >{p}</button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg) => (
            <div key={msg.id} style={{ display: "flex", flexDirection: "column", alignItems: msg.role === "user" ? "flex-end" : "flex-start" }}>
              <div style={{ position: "relative", maxWidth: "85%", padding: "10px 14px", borderRadius: msg.role === "user" ? "12px 12px 2px 12px" : "12px 12px 12px 2px", background: msg.role === "user" ? theme.userBubble : "var(--surface2)", color: msg.role === "user" ? "white" : "var(--text)", fontSize: "13px", lineHeight: "1.5", whiteSpace: "pre-wrap" }}>
                {msg.content}
                {/* Copy button on hover */}
                <button onClick={() => copyMessage(msg)} style={{ position: "absolute", top: "4px", right: "4px", width: "22px", height: "22px", borderRadius: "4px", border: "none", background: msg.role === "user" ? "rgba(255,255,255,0.2)" : "var(--surface)", color: msg.role === "user" ? "white" : "var(--text2)", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", opacity: 0.6, transition: "opacity 0.12s", fontSize: "10px" }}
                  onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.6"; }}
                  title="Copy message"
                >
                  {copiedId === msg.id ? <Check size={11} /> : <Copy size={11} />}
                </button>
              </div>
              {msg.model && <div style={{ fontSize: "9px", color: "var(--text3)", marginTop: "3px", paddingLeft: "4px" }}>{msg.model}</div>}
            </div>
          ))}

          {loading && (
            <div style={{ display: "flex", alignItems: "center", gap: "6px", color: "var(--text2)", fontSize: "12px" }}>
              <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> Thinking...
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div style={{ padding: "10px 14px", borderTop: "1px solid var(--border)", display: "flex", gap: "8px", flexShrink: 0, background: "var(--surface)" }}>
          <input type="text" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && sendMessage()} placeholder={`Ask Cilantro...`} disabled={loading}
            style={{ flex: 1, padding: "10px 14px", borderRadius: "10px", border: "1px solid var(--border)", background: "var(--surface2)", color: "var(--text)", fontSize: "13px", outline: "none" }}
          />
          <button onClick={() => sendMessage()} disabled={loading || !input.trim()} style={{ width: "38px", height: "38px", borderRadius: "10px", border: "none", background: theme.accent, color: "white", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", opacity: (loading || !input.trim()) ? 0.5 : 1 }}>
            <Send size={16} />
          </button>
        </div>
      </>)}
    </div>
  );
}
