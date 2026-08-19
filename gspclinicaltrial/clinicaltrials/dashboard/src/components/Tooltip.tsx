import { useState, useRef } from "react";
import { HelpCircle } from "lucide-react";

interface TooltipProps {
  content: string;
  steps?: string[];
  position?: "top" | "bottom" | "left" | "right";
  children?: React.ReactNode;
}

export default function Tooltip({ content, steps, position = "top", children }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const posStyle: Record<string, React.CSSProperties> = {
    top: { bottom: "calc(100% + 8px)", left: "50%", transform: "translateX(-50%)" },
    bottom: { top: "calc(100% + 8px)", left: "50%", transform: "translateX(-50%)" },
    left: { right: "calc(100% + 8px)", top: "50%", transform: "translateY(-50%)" },
    right: { left: "calc(100% + 8px)", top: "50%", transform: "translateY(-50%)" },
  };

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex" }} onMouseEnter={() => setVisible(true)} onMouseLeave={() => setVisible(false)}>
      {children || <HelpCircle size={13} style={{ color: "var(--text2)", cursor: "help" }} />}
      {visible && (
        <div style={{
          position: "absolute",
          ...posStyle[position],
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "8px",
          padding: "10px 14px",
          boxShadow: "0 8px 24px rgba(0,0,0,0.15)",
          zIndex: 9999,
          minWidth: "220px",
          maxWidth: "320px",
          fontSize: "12px",
          color: "var(--text)",
          lineHeight: "1.5",
          pointerEvents: "none",
        }}>
          <div style={{ fontWeight: 600, marginBottom: steps ? "6px" : 0 }}>{content}</div>
          {steps && (
            <ol style={{ margin: 0, paddingLeft: "16px", color: "var(--text2)" }}>
              {steps.map((s, i) => <li key={i} style={{ marginBottom: "3px" }}>{s}</li>)}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
