import { useState, useCallback, useRef, useEffect } from "react";
import { Code, Trash2, RotateCcw, Download, Workflow, Plus, Save, Edit3, X, Copy, User, ChevronDown, Settings } from "lucide-react";
import styles from "./FlowEditor.module.css";
import NodeManager, { type ManagedNode, loadManagedNodes, saveManagedNodes } from "./NodeManager";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface FlowNode {
  id: string;
  name: string;
  description: string;
  category: "data" | "process" | "decision" | "action" | "terminal";
  x: number;
  y: number;
  functionBody?: string;
}

export interface FlowEdge {
  id: string;
  from: string;
  to: string;
  condition?: string;
  label?: string;
}

interface DragState {
  nodeId: string;
  offsetX: number;
  offsetY: number;
}

interface ConnectState {
  fromId: string;
  mouseX: number;
  mouseY: number;
}

// ─── Palette Profile Types ───────────────────────────────────────────────────

interface PaletteItem {
  name: string;
  description: string;
  icon: string;
}

interface PaletteCategory {
  category: "data" | "process" | "decision" | "action" | "terminal";
  items: PaletteItem[];
}

interface PaletteProfile {
  id: string;
  name: string;
  description: string;
  createdAt: string;
  updatedAt: string;
  categories: PaletteCategory[];
}

const CATEGORY_LABELS: Record<string, string> = {
  data: "Data Loading",
  process: "Processing",
  decision: "Decision",
  action: "Actions",
  terminal: "Terminal",
};

const ICON_OPTIONS = ["📋", "📝", "💬", "✓", "🔍", "⚡", "☑", "⚖", "🔬", "❓", "📌", "⊘", "🚨", "✅", "⚙", "🧠", "📊", "🔗", "🛡", "📡", "🗄", "💾", "🔄", "⏱", "🎯", "📦", "🧪", "🔧"];

// ─── Built-in palette (default profile) ──────────────────────────────────────

const DEFAULT_PALETTE: PaletteCategory[] = [
  { category: "data", items: [
    { name: "load_patient", description: "Load patient FHIR data from HealthLake", icon: "📋" },
    { name: "load_questionnaire", description: "Load trial FHIR Questionnaire", icon: "📝" },
  ]},
  { category: "process", items: [
    { name: "ask_question", description: "Generate conversational question", icon: "💬" },
    { name: "validate_response", description: "Validate patient response", icon: "✓" },
    { name: "check_discrepancy", description: "Compare with FHIR records", icon: "🔍" },
    { name: "check_enable_when", description: "Evaluate conditional logic", icon: "⚡" },
    { name: "check_completion", description: "Verify all items answered", icon: "☑" },
  ]},
  { category: "decision", items: [
    { name: "resolve_eligibility", description: "Determine eligibility", icon: "⚖" },
    { name: "deep_dive_module", description: "Activate deep-dive questions", icon: "🔬" },
  ]},
  { category: "action", items: [
    { name: "ask_clarification", description: "Request clarification", icon: "❓" },
    { name: "prompt_missing", description: "Prompt for missing items", icon: "📌" },
    { name: "record_declined", description: "Record declined items", icon: "⊘" },
    { name: "escalate", description: "Escalate to PI review", icon: "🚨" },
  ]},
  { category: "terminal", items: [
    { name: "finalize_screening", description: "Persist results to HealthLake", icon: "✅" },
    { name: "custom_node", description: "Custom processing node", icon: "⚙" },
  ]},
];

const BUILT_IN_PROFILE: PaletteProfile = {
  id: "__default__",
  name: "Patient-to-Trial Matching",
  description: "Default recruitment workflow nodes",
  createdAt: "2024-01-01T00:00:00Z",
  updatedAt: "2024-01-01T00:00:00Z",
  categories: DEFAULT_PALETTE,
};

// ─── LocalStorage helpers ────────────────────────────────────────────────────

const STORAGE_KEY = "flow-editor-palette-profiles";
const ACTIVE_PROFILE_KEY = "flow-editor-active-profile";

function loadProfiles(): PaletteProfile[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function saveProfiles(profiles: PaletteProfile[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(profiles));
}

function loadActiveProfileId(): string {
  return localStorage.getItem(ACTIVE_PROFILE_KEY) || "__default__";
}

function saveActiveProfileId(id: string) {
  localStorage.setItem(ACTIVE_PROFILE_KEY, id);
}

// ─── Default screening flow ─────────────────────────────────────────────────

function defaultNodes(): FlowNode[] {
  return [
    { id: "n1", name: "load_patient", description: "Load patient FHIR data", category: "data", x: 340, y: 40 },
    { id: "n2", name: "load_questionnaire", description: "Load trial Questionnaire", category: "data", x: 340, y: 130 },
    { id: "n3", name: "ask_question", description: "Generate question", category: "process", x: 340, y: 220 },
    { id: "n4", name: "validate_response", description: "Validate response", category: "process", x: 340, y: 310 },
    { id: "n5", name: "check_discrepancy", description: "Check FHIR records", category: "process", x: 340, y: 400 },
    { id: "n6", name: "ask_clarification", description: "Request clarification", category: "action", x: 580, y: 355 },
    { id: "n7", name: "check_enable_when", description: "Evaluate conditions", category: "process", x: 340, y: 490 },
    { id: "n8", name: "deep_dive_module", description: "Deep-dive questions", category: "decision", x: 580, y: 490 },
    { id: "n9", name: "check_completion", description: "All items answered?", category: "process", x: 340, y: 580 },
    { id: "n10", name: "prompt_missing", description: "Prompt missing items", category: "action", x: 580, y: 580 },
    { id: "n11", name: "record_declined", description: "Record declined", category: "action", x: 120, y: 310 },
    { id: "n12", name: "resolve_eligibility", description: "Determine eligibility", category: "decision", x: 340, y: 670 },
    { id: "n13", name: "escalate", description: "Escalate to PI", category: "action", x: 580, y: 670 },
    { id: "n14", name: "finalize_screening", description: "Persist results", category: "terminal", x: 340, y: 760 },
  ];
}

function defaultEdges(): FlowEdge[] {
  return [
    { id: "e1", from: "n1", to: "n2" },
    { id: "e2", from: "n2", to: "n3" },
    { id: "e3", from: "n3", to: "n4", label: "response" },
    { id: "e4", from: "n4", to: "n5", label: "valid" },
    { id: "e5", from: "n4", to: "n6", condition: "invalid", label: "invalid" },
    { id: "e6", from: "n4", to: "n11", condition: "declined", label: "declined" },
    { id: "e7", from: "n5", to: "n7", label: "ok" },
    { id: "e8", from: "n5", to: "n6", condition: "critical", label: "critical" },
    { id: "e9", from: "n6", to: "n4" },
    { id: "e10", from: "n7", to: "n3", condition: "more_items", label: "next" },
    { id: "e11", from: "n7", to: "n8", condition: "deep_dive", label: "deep dive" },
    { id: "e12", from: "n7", to: "n9", label: "done" },
    { id: "e13", from: "n8", to: "n3" },
    { id: "e14", from: "n9", to: "n12", label: "complete" },
    { id: "e15", from: "n9", to: "n10", condition: "missing", label: "missing" },
    { id: "e16", from: "n10", to: "n4" },
    { id: "e17", from: "n11", to: "n9" },
    { id: "e18", from: "n12", to: "n14", label: "eligible/ineligible" },
    { id: "e19", from: "n12", to: "n13", condition: "borderline", label: "borderline" },
    { id: "e20", from: "n13", to: "n14" },
  ];
}

// ─── LangGraph code generator ────────────────────────────────────────────────

function generateLangGraphCode(nodes: FlowNode[], edges: FlowEdge[]): string {
  const lines: string[] = [];
  lines.push('"""Auto-generated LangGraph StateGraph from Flow Editor."""');
  lines.push("");
  lines.push("from langgraph.graph import END, StateGraph");
  lines.push("from models.state import ScreeningState");
  lines.push("");
  lines.push("");

  for (const node of nodes) {
    const fnName = node.name.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
    lines.push(`def ${fnName}_node(state: ScreeningState) -> dict:`);
    lines.push(`    """${node.description}"""`);
    if (node.functionBody) {
      for (const bodyLine of node.functionBody.split("\n")) {
        lines.push(`    ${bodyLine}`);
      }
    } else {
      lines.push("    # TODO: implement");
      lines.push("    return {}");
    }
    lines.push("");
    lines.push("");
  }

  const conditionalSources = new Map<string, FlowEdge[]>();
  for (const edge of edges) {
    if (edge.condition) {
      const existing = conditionalSources.get(edge.from) || [];
      existing.push(edge);
      conditionalSources.set(edge.from, existing);
    }
  }

  for (const [sourceId, condEdges] of conditionalSources) {
    const sourceNode = nodes.find((n) => n.id === sourceId);
    if (!sourceNode) continue;
    const fnName = sourceNode.name.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
    lines.push(`def _route_after_${fnName}(state: ScreeningState) -> str:`);
    lines.push(`    """Conditional routing after ${sourceNode.name}."""`);
    for (const ce of condEdges) {
      const targetNode = nodes.find((n) => n.id === ce.to);
      if (!targetNode) continue;
      lines.push(`    # Condition: ${ce.condition}`);
      lines.push(`    # if <condition>:`);
      lines.push(`    #     return "${targetNode.name}"`);
    }
    const defaultEdge = edges.find((e) => e.from === sourceId && !e.condition);
    if (defaultEdge) {
      const defaultTarget = nodes.find((n) => n.id === defaultEdge.to);
      if (defaultTarget) lines.push(`    return "${defaultTarget.name}"`);
    } else {
      lines.push('    return "' + (condEdges[0] ? nodes.find(n => n.id === condEdges[0].to)?.name || "END" : "END") + '"');
    }
    lines.push("");
    lines.push("");
  }

  lines.push("def build_screening_graph() -> StateGraph:");
  lines.push('    """Build the Screening Agent StateGraph."""');
  lines.push("    graph = StateGraph(ScreeningState)");
  lines.push("");

  for (const node of nodes) {
    const fnName = node.name.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
    lines.push(`    graph.add_node("${node.name}", ${fnName}_node)`);
  }
  lines.push("");

  if (nodes.length > 0) {
    lines.push(`    graph.set_entry_point("${nodes[0].name}")`);
    lines.push("");
  }

  const processedSources = new Set<string>();
  for (const edge of edges) {
    if (edge.condition) continue;
    if (conditionalSources.has(edge.from)) continue;
    const fromNode = nodes.find((n) => n.id === edge.from);
    const toNode = nodes.find((n) => n.id === edge.to);
    if (!fromNode || !toNode) continue;
    lines.push(`    graph.add_edge("${fromNode.name}", "${toNode.name}")`);
  }
  lines.push("");

  for (const [sourceId, condEdges] of conditionalSources) {
    const sourceNode = nodes.find((n) => n.id === sourceId);
    if (!sourceNode || processedSources.has(sourceId)) continue;
    processedSources.add(sourceId);
    const fnName = sourceNode.name.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
    const routeMap: Record<string, string> = {};
    for (const ce of condEdges) {
      const targetNode = nodes.find((n) => n.id === ce.to);
      if (targetNode) routeMap[targetNode.name] = targetNode.name;
    }
    const defaultEdge = edges.find((e) => e.from === sourceId && !e.condition);
    if (defaultEdge) {
      const defaultTarget = nodes.find((n) => n.id === defaultEdge.to);
      if (defaultTarget) routeMap[defaultTarget.name] = defaultTarget.name;
    }
    const mapStr = Object.entries(routeMap).map(([k, v]) => `        "${k}": "${v}"`).join(",\n");
    lines.push(`    graph.add_conditional_edges("${sourceNode.name}", _route_after_${fnName}, {`);
    lines.push(mapStr);
    lines.push("    })");
    lines.push("");
  }

  const lastNode = nodes[nodes.length - 1];
  if (lastNode) lines.push(`    graph.add_edge("${lastNode.name}", END)`);
  lines.push("");
  lines.push("    return graph");
  lines.push("");
  lines.push("");
  lines.push("def compile_screening_graph():");
  lines.push('    """Build and compile the graph."""');
  lines.push("    graph = build_screening_graph()");
  lines.push("    return graph.compile()");
  lines.push("");
  lines.push("");
  lines.push("screening_app = compile_screening_graph()");

  return lines.join("\n");
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function FlowEditor() {
  const [nodes, setNodes] = useState<FlowNode[]>(defaultNodes);
  const [edges, setEdges] = useState<FlowEdge[]>(defaultEdges);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [showCode, setShowCode] = useState(false);
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [connectState, setConnectState] = useState<ConnectState | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(15);

  // ─── Palette Profile State ─────────────────────────────────────────────────
  const [customProfiles, setCustomProfiles] = useState<PaletteProfile[]>(loadProfiles);
  const [activeProfileId, setActiveProfileId] = useState<string>(loadActiveProfileId);
  const [showProfileMenu, setShowProfileMenu] = useState(false);
  const [showProfileEditor, setShowProfileEditor] = useState(false);
  const [editingProfile, setEditingProfile] = useState<PaletteProfile | null>(null);
  const [showAddNode, setShowAddNode] = useState(false);
  const [addNodeCategory, setAddNodeCategory] = useState<"data" | "process" | "decision" | "action" | "terminal">("process");
  const [addNodeName, setAddNodeName] = useState("");
  const [addNodeDesc, setAddNodeDesc] = useState("");
  const [addNodeIcon, setAddNodeIcon] = useState("⚙");

  // ─── Node Manager State ────────────────────────────────────────────────────
  const [showNodeManager, setShowNodeManager] = useState(false);
  const [managedNodes, setManagedNodes] = useState<ManagedNode[]>(loadManagedNodes);

  const handleManagedNodesChange = useCallback((nodes: ManagedNode[]) => {
    setManagedNodes(nodes);
    saveManagedNodes(nodes);
  }, []);

  const handleAddToProfileFromManager = useCallback((node: ManagedNode) => {
    if (activeProfileId === "__default__") return;
    setCustomProfiles((prev) =>
      prev.map((p) => {
        if (p.id !== activeProfileId) return p;
        const cats = p.categories.map((c) => {
          if (c.category !== node.category) return c;
          if (c.items.some((i) => i.name === node.name)) return c;
          return { ...c, items: [...c.items, { name: node.name, description: node.description, icon: node.icon }] };
        });
        return { ...p, categories: cats, updatedAt: new Date().toISOString() };
      })
    );
  }, [activeProfileId]);

  const allProfiles = [BUILT_IN_PROFILE, ...customProfiles];
  const activeProfile = allProfiles.find((p) => p.id === activeProfileId) || BUILT_IN_PROFILE;
  const paletteCategories = activeProfile.categories;

  // Persist profile changes
  useEffect(() => { saveProfiles(customProfiles); }, [customProfiles]);
  useEffect(() => { saveActiveProfileId(activeProfileId); }, [activeProfileId]);

  const selectedNode = nodes.find((n) => n.id === selectedNodeId) || null;
  const selectedEdge = edges.find((e) => e.id === selectedEdgeId) || null;

  // ─── Profile Management ────────────────────────────────────────────────────

  const createProfile = useCallback((name: string, description: string) => {
    const profile: PaletteProfile = {
      id: `profile-${Date.now()}`,
      name,
      description,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      categories: [
        { category: "data", items: [] },
        { category: "process", items: [] },
        { category: "decision", items: [] },
        { category: "action", items: [] },
        { category: "terminal", items: [] },
      ],
    };
    setCustomProfiles((prev) => [...prev, profile]);
    setActiveProfileId(profile.id);
    setShowProfileEditor(false);
  }, []);

  const duplicateProfile = useCallback((sourceId: string) => {
    const allProfilesSnapshot = [BUILT_IN_PROFILE, ...customProfiles];
    const source = allProfilesSnapshot.find((p) => p.id === sourceId);
    if (!source) return;
    const profile: PaletteProfile = {
      ...structuredClone(source),
      id: `profile-${Date.now()}`,
      name: `${source.name} (Copy)`,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    setCustomProfiles((prev) => [...prev, profile]);
    setActiveProfileId(profile.id);
  }, [customProfiles]);

  const deleteProfile = useCallback((profileId: string) => {
    if (profileId === "__default__") return;
    setCustomProfiles((prev) => prev.filter((p) => p.id !== profileId));
    setActiveProfileId((prev) => prev === profileId ? "__default__" : prev);
  }, []);

  const renameProfile = useCallback((profileId: string, name: string, description: string) => {
    setCustomProfiles((prev) =>
      prev.map((p) => p.id === profileId ? { ...p, name, description, updatedAt: new Date().toISOString() } : p)
    );
  }, []);

  const addNodeToProfile = useCallback(() => {
    if (!addNodeName.trim() || activeProfileId === "__default__") return;
    const nodeName = addNodeName.trim().replace(/\s+/g, "_").toLowerCase();
    setCustomProfiles((prev) =>
      prev.map((p) => {
        if (p.id !== activeProfileId) return p;
        const cats = p.categories.map((c) => {
          if (c.category !== addNodeCategory) return c;
          return { ...c, items: [...c.items, { name: nodeName, description: addNodeDesc, icon: addNodeIcon }] };
        });
        return { ...p, categories: cats, updatedAt: new Date().toISOString() };
      })
    );
    setAddNodeName("");
    setAddNodeDesc("");
    setAddNodeIcon("⚙");
    setShowAddNode(false);
  }, [addNodeName, addNodeDesc, addNodeIcon, addNodeCategory, activeProfileId]);

  const removeNodeFromProfile = useCallback((category: string, nodeName: string) => {
    if (activeProfileId === "__default__") return;
    setCustomProfiles((prev) =>
      prev.map((p) => {
        if (p.id !== activeProfileId) return p;
        const cats = p.categories.map((c) => {
          if (c.category !== category) return c;
          return { ...c, items: c.items.filter((i) => i.name !== nodeName) };
        });
        return { ...p, categories: cats, updatedAt: new Date().toISOString() };
      })
    );
  }, [activeProfileId]);

  // ─── Drag nodes ──────────────────────────────────────────────────────────

  const handleNodeMouseDown = useCallback(
    (e: React.MouseEvent, nodeId: string) => {
      if ((e.target as HTMLElement).classList.contains(styles.port)) return;
      e.preventDefault();
      const node = nodes.find((n) => n.id === nodeId);
      if (!node) return;
      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;
      setDragState({ nodeId, offsetX: e.clientX - rect.left - node.x, offsetY: e.clientY - rect.top - node.y });
      setSelectedNodeId(nodeId);
      setSelectedEdgeId(null);
    },
    [nodes]
  );

  useEffect(() => {
    if (!dragState) return;
    const handleMove = (e: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;
      setNodes((prev) => prev.map((n) => (n.id === dragState.nodeId ? { ...n, x: Math.max(0, e.clientX - rect.left - dragState.offsetX), y: Math.max(0, e.clientY - rect.top - dragState.offsetY) } : n)));
    };
    const handleUp = () => setDragState(null);
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => { window.removeEventListener("mousemove", handleMove); window.removeEventListener("mouseup", handleUp); };
  }, [dragState]);

  // ─── Connect nodes ───────────────────────────────────────────────────────

  const handlePortMouseDown = useCallback((e: React.MouseEvent, nodeId: string) => {
    e.preventDefault();
    e.stopPropagation();
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    setConnectState({ fromId: nodeId, mouseX: e.clientX - rect.left, mouseY: e.clientY - rect.top });
  }, []);

  useEffect(() => {
    if (!connectState) return;
    const handleMove = (e: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;
      setConnectState((prev) => prev ? { ...prev, mouseX: e.clientX - rect.left, mouseY: e.clientY - rect.top } : null);
    };
    const handleUp = (e: MouseEvent) => {
      const target = document.elementFromPoint(e.clientX, e.clientY);
      if (target) {
        const toId = target.getAttribute("data-port-in");
        if (!toId) {
          setConnectState(null);
          return;
        }
        if (toId !== connectState.fromId) {
          setEdges((prev) => [...prev, { id: `e${nextId.current++}`, from: connectState.fromId, to: toId }]);
        }
      }
      setConnectState(null);
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => { window.removeEventListener("mousemove", handleMove); window.removeEventListener("mouseup", handleUp); };
  }, [connectState]);

  // ─── Drop from palette ───────────────────────────────────────────────────

  const handleCanvasDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const data = e.dataTransfer.getData("application/flow-node");
    if (!data) return;
    const { name, description, category } = JSON.parse(data);
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const id = `n${nextId.current++}`;
    setNodes((prev) => [...prev, { id, name, description, category, x: e.clientX - rect.left - 80, y: e.clientY - rect.top - 30 }]);
    setSelectedNodeId(id);
  }, []);

  const handleCanvasDragOver = useCallback((e: React.DragEvent) => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; }, []);

  // ─── Actions ─────────────────────────────────────────────────────────────

  const deleteSelected = useCallback(() => {
    if (selectedNodeId) {
      setNodes((prev) => prev.filter((n) => n.id !== selectedNodeId));
      setEdges((prev) => prev.filter((e) => e.from !== selectedNodeId && e.to !== selectedNodeId));
      setSelectedNodeId(null);
    } else if (selectedEdgeId) {
      setEdges((prev) => prev.filter((e) => e.id !== selectedEdgeId));
      setSelectedEdgeId(null);
    }
  }, [selectedNodeId, selectedEdgeId]);

  const resetFlow = useCallback(() => { setNodes(defaultNodes()); setEdges(defaultEdges()); setSelectedNodeId(null); setSelectedEdgeId(null); nextId.current = 15; }, []);

  const updateNodeProp = useCallback((key: keyof FlowNode, value: string) => {
    if (!selectedNodeId) return;
    setNodes((prev) => prev.map((n) => (n.id === selectedNodeId ? { ...n, [key]: value } : n)));
  }, [selectedNodeId]);

  const updateEdgeProp = useCallback((key: keyof FlowEdge, value: string) => {
    if (!selectedEdgeId) return;
    setEdges((prev) => prev.map((e) => (e.id === selectedEdgeId ? { ...e, [key]: value || undefined } : e)));
  }, [selectedEdgeId]);

  const handleEdgeClick = useCallback((edgeId: string) => { setSelectedEdgeId(edgeId); setSelectedNodeId(null); }, []);
  const handleCanvasClick = useCallback((e: React.MouseEvent) => {
    if (e.target === canvasRef.current || (e.target as HTMLElement).tagName === "svg") { setSelectedNodeId(null); setSelectedEdgeId(null); }
  }, []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Delete" || e.key === "Backspace") {
        if (document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA") return;
        deleteSelected();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [deleteSelected]);

  // ─── Edge paths ──────────────────────────────────────────────────────────

  const getNodeBottom = (nodeId: string) => { const node = nodes.find((n) => n.id === nodeId); if (!node) return { x: 0, y: 0 }; return { x: node.x + 80, y: node.y + 60 }; };
  const getNodeTop = (nodeId: string) => { const node = nodes.find((n) => n.id === nodeId); if (!node) return { x: 0, y: 0 }; return { x: node.x + 80, y: node.y }; };
  const getEdgePath = (edge: FlowEdge) => { const from = getNodeBottom(edge.from); const to = getNodeTop(edge.to); const cy1 = from.y + Math.min(40, Math.abs(to.y - from.y) * 0.4); const cy2 = to.y - Math.min(40, Math.abs(to.y - from.y) * 0.4); const cx = from.x + (to.x - from.x) * 0.5; return `M ${from.x} ${from.y} C ${cx} ${cy1}, ${cx} ${cy2}, ${to.x} ${to.y}`; };
  const getEdgeMidpoint = (edge: FlowEdge) => { const from = getNodeBottom(edge.from); const to = getNodeTop(edge.to); return { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 }; };

  const generatedCode = showCode ? generateLangGraphCode(nodes, edges) : "";
  const allPaletteItems = paletteCategories.flatMap((g) => g.items);

  // ─── Render ──────────────────────────────────────────────────────────────

  return (
    <div className={styles.container}>
      {/* Sidebar - Node Palette with Profile Management */}
      <div className={styles.sidebar}>
        {/* Profile Selector */}
        <div className={styles.profileSelector}>
          <button className={styles.profileBtn} onClick={() => setShowProfileMenu(!showProfileMenu)}>
            <User size={12} />
            <span className={styles.profileName}>{activeProfile.name}</span>
            <ChevronDown size={12} />
          </button>
          {showProfileMenu && (
            <div className={styles.profileDropdown}>
              {allProfiles.map((p) => (
                <div key={p.id} className={`${styles.profileOption} ${p.id === activeProfileId ? styles.profileOptionActive : ""}`}>
                  <button className={styles.profileOptionBtn} onClick={() => { setActiveProfileId(p.id); setShowProfileMenu(false); }}>
                    {p.name}
                    {p.id === "__default__" && <span className={styles.profileBadge}>Built-in</span>}
                  </button>
                  {p.id !== "__default__" && (
                    <div className={styles.profileOptionActions}>
                      <button onClick={() => { setEditingProfile(p); setShowProfileEditor(true); setShowProfileMenu(false); }} title="Edit"><Edit3 size={11} /></button>
                      <button onClick={() => duplicateProfile(p.id)} title="Duplicate"><Copy size={11} /></button>
                      <button onClick={() => deleteProfile(p.id)} title="Delete" className={styles.profileDeleteBtn}><Trash2 size={11} /></button>
                    </div>
                  )}
                  {p.id === "__default__" && (
                    <button className={styles.profileDupeBtn} onClick={() => duplicateProfile(p.id)} title="Duplicate as custom"><Copy size={11} /></button>
                  )}
                </div>
              ))}
              <div className={styles.profileDropdownFooter}>
                <button className={styles.profileCreateBtn} onClick={() => { setEditingProfile(null); setShowProfileEditor(true); setShowProfileMenu(false); }}>
                  <Plus size={12} /> New Profile
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Profile Editor Modal */}
        {showProfileEditor && (
          <ProfileEditorModal
            profile={editingProfile}
            onSave={(name, desc) => {
              if (editingProfile) { renameProfile(editingProfile.id, name, desc); }
              else { createProfile(name, desc); }
              setShowProfileEditor(false);
            }}
            onClose={() => setShowProfileEditor(false)}
          />
        )}

        {/* Palette nodes */}
        <div className={styles.sidebarHeader}>
          <Workflow size={14} style={{ marginRight: 6, verticalAlign: "middle" }} />
          Node Palette
          <button className={styles.addNodeBtn} onClick={() => setShowNodeManager(true)} title="Manage Nodes">
            <Settings size={12} />
          </button>
          {activeProfileId !== "__default__" && (
            <button className={styles.addNodeBtn} onClick={() => setShowAddNode(true)} title="Add custom node">
              <Plus size={12} />
            </button>
          )}
        </div>

        {/* Add Node Form */}
        {showAddNode && activeProfileId !== "__default__" && (
          <div className={styles.addNodeForm}>
            <div className={styles.addNodeFormTitle}>Add Custom Node</div>
            <input className={styles.addNodeInput} placeholder="Node name" value={addNodeName} onChange={(e) => setAddNodeName(e.target.value)} />
            <input className={styles.addNodeInput} placeholder="Description" value={addNodeDesc} onChange={(e) => setAddNodeDesc(e.target.value)} />
            <div className={styles.addNodeRow}>
              <select className={styles.addNodeSelect} value={addNodeCategory} onChange={(e) => setAddNodeCategory(e.target.value as typeof addNodeCategory)}>
                <option value="data">Data</option>
                <option value="process">Process</option>
                <option value="decision">Decision</option>
                <option value="action">Action</option>
                <option value="terminal">Terminal</option>
              </select>
              <div className={styles.iconPicker}>
                {ICON_OPTIONS.slice(0, 14).map((ic) => (
                  <button key={ic} className={`${styles.iconOption} ${addNodeIcon === ic ? styles.iconOptionActive : ""}`} onClick={() => setAddNodeIcon(ic)}>{ic}</button>
                ))}
              </div>
            </div>
            <div className={styles.addNodeActions}>
              <button className={styles.addNodeSaveBtn} onClick={addNodeToProfile} disabled={!addNodeName.trim()}>
                <Save size={12} /> Add Node
              </button>
              <button className={styles.addNodeCancelBtn} onClick={() => setShowAddNode(false)}>Cancel</button>
            </div>
          </div>
        )}

        {paletteCategories.map((group) => (
          <div key={group.category}>
            {group.items.length > 0 && (
              <>
                <div className={styles.nodeCategory}>{CATEGORY_LABELS[group.category]}</div>
                <div className={styles.nodePalette}>
                  {group.items.map((item) => (
                    <div key={item.name} className={styles.paletteNode} draggable
                      onDragStart={(e) => {
                        e.dataTransfer.setData("application/flow-node", JSON.stringify({ name: item.name, description: item.description, category: group.category }));
                        e.dataTransfer.effectAllowed = "copy";
                      }}
                    >
                      <span className={`${styles.paletteIcon} ${styles[group.category]}`}>{item.icon}</span>
                      <span className={styles.paletteNodeName}>{item.name.replace(/_/g, " ")}</span>
                      {activeProfileId !== "__default__" && (
                        <button className={styles.paletteNodeRemove} onClick={(e) => { e.stopPropagation(); removeNodeFromProfile(group.category, item.name); }} title="Remove from palette">
                          <X size={10} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        ))}
      </div>

      {/* Canvas Area */}
      <div className={styles.canvasArea}>
        <div className={styles.toolbar}>
          <button className={`${styles.toolbarBtn} ${styles.primary}`} onClick={() => setShowCode(!showCode)}>
            <Code size={14} /> {showCode ? "Hide Implementation" : "View Implementation"}
          </button>
          <button className={styles.toolbarBtn} onClick={resetFlow}><RotateCcw size={14} /> Reset</button>
          <button className={`${styles.toolbarBtn} ${styles.danger}`} onClick={deleteSelected} disabled={!selectedNodeId && !selectedEdgeId}>
            <Trash2 size={14} /> Delete
          </button>
          <div className={styles.toolbarSpacer} />
          <span className={styles.toolbarLabel}>{nodes.length} nodes &middot; {edges.length} edges</span>
          {showCode && (
            <button className={styles.toolbarBtn} onClick={() => { const blob = new Blob([generatedCode], { type: "text/plain" }); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = "screening_graph.py"; a.click(); URL.revokeObjectURL(url); }}>
              <Download size={14} /> Download .py
            </button>
          )}
        </div>

        <div ref={canvasRef} className={styles.canvas} onDrop={handleCanvasDrop} onDragOver={handleCanvasDragOver} onClick={handleCanvasClick}>
          {nodes.length === 0 && (
            <div className={styles.emptyCanvas}>
              <Workflow size={40} />
              <h3>Design Your Screening Flow</h3>
              <p>Drag nodes from the palette on the left and connect them to model the recruitment workflow.</p>
            </div>
          )}

          <svg className={styles.canvasSvg}>
            <defs>
              <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="var(--text2)" /></marker>
              <marker id="arrowhead-cond" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#d97706" /></marker>
            </defs>
            {edges.map((edge) => { const mid = getEdgeMidpoint(edge); return (
              <g key={edge.id}>
                <path d={getEdgePath(edge)} className={`${styles.edgeLine} ${edge.condition ? styles.conditional : ""}`} markerEnd={edge.condition ? "url(#arrowhead-cond)" : "url(#arrowhead)"} onClick={(e) => { e.stopPropagation(); handleEdgeClick(edge.id); }} style={{ pointerEvents: "stroke", cursor: "pointer" }} />
                {edge.label && <text x={mid.x} y={mid.y - 6} className={styles.edgeLabel}>{edge.label}</text>}
              </g>
            ); })}
            {connectState && <line x1={getNodeBottom(connectState.fromId).x} y1={getNodeBottom(connectState.fromId).y} x2={connectState.mouseX} y2={connectState.mouseY} stroke="var(--accent)" strokeWidth={2} strokeDasharray="4 4" />}
          </svg>

          {nodes.map((node, idx) => (
            <div key={node.id} className={`${styles.canvasNode} ${node.id === selectedNodeId ? styles.selected : ""} ${idx === 0 ? styles.entry : ""} ${idx === nodes.length - 1 ? styles.end : ""}`} style={{ left: node.x, top: node.y }} onMouseDown={(e) => handleNodeMouseDown(e, node.id)}>
              <div className={`${styles.port} ${styles.portIn}`} data-port-in={node.id} />
              <div className={styles.nodeHeader}>
                <span className={`${styles.nodeIcon} ${styles[node.category]}`}>{allPaletteItems.find((i) => i.name === node.name)?.icon || "⚙"}</span>
                <span className={styles.nodeName}>{node.name.replace(/_/g, " ")}</span>
              </div>
              <div className={styles.nodeDesc}>{node.description}</div>
              <div className={`${styles.port} ${styles.portOut}`} onMouseDown={(e) => handlePortMouseDown(e, node.id)} />
            </div>
          ))}
        </div>
      </div>

      {/* Properties Panel */}
      {(selectedNode || selectedEdge) && !showCode && (
        <div className={styles.propsPanel}>
          <div className={styles.propsPanelHeader}>
            {selectedNode ? "Node Properties" : "Edge Properties"}
            <button className={styles.propsClose} onClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); }}>&times;</button>
          </div>
          <div className={styles.propsBody}>
            {selectedNode && (<>
              <div className={styles.propGroup}><label className={styles.propLabel}>Node Name</label><input className={styles.propInput} value={selectedNode.name} onChange={(e) => updateNodeProp("name", e.target.value)} /></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>Description</label><input className={styles.propInput} value={selectedNode.description} onChange={(e) => updateNodeProp("description", e.target.value)} /></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>Category</label><select className={styles.propSelect} value={selectedNode.category} onChange={(e) => updateNodeProp("category", e.target.value)}><option value="data">Data Loading</option><option value="process">Processing</option><option value="decision">Decision</option><option value="action">Action</option><option value="terminal">Terminal</option></select></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>Function Body (Python)</label><textarea className={styles.propTextarea} value={selectedNode.functionBody || ""} onChange={(e) => updateNodeProp("functionBody", e.target.value)} placeholder="# Python code for this node..." rows={6} /></div>
            </>)}
            {selectedEdge && (<>
              <div className={styles.propGroup}><label className={styles.propLabel}>Label</label><input className={styles.propInput} value={selectedEdge.label || ""} onChange={(e) => updateEdgeProp("label", e.target.value)} placeholder="Edge label" /></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>Condition (makes edge conditional/dashed)</label><input className={styles.propInput} value={selectedEdge.condition || ""} onChange={(e) => updateEdgeProp("condition", e.target.value)} placeholder="e.g. borderline, critical, invalid" /></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>From</label><input className={styles.propInput} value={nodes.find(n => n.id === selectedEdge.from)?.name || selectedEdge.from} disabled /></div>
              <div className={styles.propGroup}><label className={styles.propLabel}>To</label><input className={styles.propInput} value={nodes.find(n => n.id === selectedEdge.to)?.name || selectedEdge.to} disabled /></div>
            </>)}
          </div>
        </div>
      )}

      {/* Code Panel */}
      {showCode && (
        <div className={styles.codePanel}>
          <div className={styles.codePanelHeader}><span className={styles.codePanelTitle}>Workflow Implementation</span></div>
          <div className={styles.codeContent}><pre className={styles.codeBlock}>{generatedCode}</pre></div>
        </div>
      )}

      {/* Node Manager */}
      <NodeManager
        open={showNodeManager}
        onClose={() => setShowNodeManager(false)}
        customNodes={managedNodes}
        onNodesChange={handleManagedNodesChange}
        onAddToProfile={handleAddToProfileFromManager}
      />
    </div>
  );
}

// ─── Profile Editor Modal ────────────────────────────────────────────────────

function ProfileEditorModal({ profile, onSave, onClose }: { profile: PaletteProfile | null; onSave: (name: string, desc: string) => void; onClose: () => void }) {
  const [name, setName] = useState(profile?.name || "");
  const [desc, setDesc] = useState(profile?.description || "");

  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <div className={styles.modalContent} onClick={(e) => e.stopPropagation()}>
        <div className={styles.modalHeader}>
          {profile ? "Edit Profile" : "Create New Profile"}
          <button className={styles.propsClose} onClick={onClose}>&times;</button>
        </div>
        <div className={styles.modalBody}>
          <div className={styles.propGroup}>
            <label className={styles.propLabel}>Profile Name</label>
            <input className={styles.propInput} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Oncology Screening, Safety Monitoring" autoFocus />
          </div>
          <div className={styles.propGroup}>
            <label className={styles.propLabel}>Description</label>
            <input className={styles.propInput} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What is this palette for?" />
          </div>
        </div>
        <div className={styles.modalFooter}>
          <button className={styles.addNodeCancelBtn} onClick={onClose}>Cancel</button>
          <button className={styles.addNodeSaveBtn} onClick={() => onSave(name, desc)} disabled={!name.trim()}>
            <Save size={12} /> {profile ? "Save Changes" : "Create Profile"}
          </button>
        </div>
      </div>
    </div>
  );
}
