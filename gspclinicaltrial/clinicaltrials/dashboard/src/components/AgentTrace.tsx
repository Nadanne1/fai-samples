import styles from "./AgentTrace.module.css";

export interface TraceEntry {
  node: string;
  description: string;
  status: "active" | "done" | "pending";
  time: string;
}

const FLOW_NODES = [
  "load_patient",
  "load_questionnaire",
  "ask_question",
  "validate_response",
  "check_discrepancy",
  "check_enable_when",
  "resolve_eligibility",
  "finalize_screening",
];

function formatNodeName(node: string): string {
  return node
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

interface AgentTraceProps {
  traces: TraceEntry[];
  activeNode: string | null;
  completedNodes: Set<string>;
}

export default function AgentTrace({ traces, activeNode, completedNodes }: AgentTraceProps) {
  return (
    <aside className={styles.panel} aria-label="Agent trace">
      <div className={styles.panelHeader}>Agent Trace</div>

      <div className={styles.traceList}>
        {traces.length === 0 ? (
          <div className={styles.empty}>No active session</div>
        ) : (
          traces.map((t, i) => (
            <div key={i} className={`${styles.traceNode} animate-in`}>
              <div className={`${styles.dot} ${styles[t.status]}`} />
              <div>
                <div className={styles.nodeName}>{formatNodeName(t.node)}</div>
                <div className={styles.nodeDesc}>{t.description}</div>
                <div className={styles.nodeTime}>{t.time}</div>
              </div>
            </div>
          ))
        )}
      </div>

      <div className={styles.flowDiagram}>
        <h4 className={styles.flowTitle}>Screening Flow</h4>
        <div className={styles.flowNodes}>
          {FLOW_NODES.map((node, i) => {
            let state = "pending";
            if (node === activeNode) state = "active";
            else if (completedNodes.has(node)) state = "done";

            return (
              <div key={node}>
                <div
                  className={`${styles.flowNode} ${styles[`flow_${state}`]}`}
                  aria-label={`${formatNodeName(node)}: ${state}`}
                >
                  {formatNodeName(node)}
                </div>
                {i < FLOW_NODES.length - 1 && (
                  <div className={styles.flowArrow}>↓</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </aside>
  );
}
