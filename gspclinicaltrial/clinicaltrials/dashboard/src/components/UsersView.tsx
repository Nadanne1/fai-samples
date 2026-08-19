import { useState, useEffect, useCallback } from "react";
import { Users, UserPlus, Shield, Trash2, Plus, X, Save, Key, RefreshCw, AlertCircle } from "lucide-react";
import * as api from "../api";
import type { DemoResetSummary } from "../api";

const API = import.meta.env.VITE_API_URL || "";
const API_KEY = import.meta.env.VITE_API_KEY || "";

function makeApiFetch(accessToken: string) {
  return async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
    const res = await fetch(`${API}/api${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { "X-Api-Key": API_KEY } : {}),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...options?.headers,
      },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error((err as { detail?: string }).detail || `HTTP ${res.status}`);
    }
    return res.json();
  };
}

interface CognitoUser {
  username: string;
  email: string;
  status: string;
  enabled: boolean;
  createdAt: string;
  groups: string[];
}

interface CognitoGroup {
  name: string;
  description: string;
  precedence: number;
  members: string[];
}

const GROUPS = ["admin", "researcher", "viewer"] as const;
type GroupName = typeof GROUPS[number];

const GROUP_COLORS: Record<GroupName, { bg: string; color: string; border: string }> = {
  admin:      { bg: "#fef3c7", color: "#92400e", border: "#fde68a" },
  researcher: { bg: "#dbeafe", color: "#1e40af", border: "#bfdbfe" },
  viewer:     { bg: "#f1f5f9", color: "#475569", border: "#e2e8f0" },
};

type Tab = "users" | "groups" | "demo";

export default function UsersView({ flows: _flows, accessToken }: { flows: { id: string; name: string }[]; accessToken: string }) { // eslint-disable-line @typescript-eslint/no-unused-vars
  const [activeTab, setActiveTab] = useState<Tab>("users");
  const [users, setUsers] = useState<CognitoUser[]>([]);
  const [groups, setGroups] = useState<CognitoGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resettingDemo, setResettingDemo] = useState(false);
  const [resetSummary, setResetSummary] = useState<DemoResetSummary | null>(null);

  // Create user form
  const [showForm, setShowForm] = useState(false);
  const [formUsername, setFormUsername] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formGroup, setFormGroup] = useState<GroupName>("viewer");
  const [formPassword, setFormPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const apiFetch = makeApiFetch(accessToken);

  const loadData = useCallback(() => {
    const fetch_ = makeApiFetch(accessToken);
    setLoading(true);
    setError(null);
    Promise.all([
      fetch_<{ users: CognitoUser[] }>("/admin/users"),
      fetch_<{ groups: CognitoGroup[] }>("/admin/groups"),
    ]).then(([uRes, gRes]) => {
      setUsers(uRes.users);
      setGroups(gRes.groups);
      setLoading(false);
    }).catch((e) => {
      setError(e instanceof Error ? e.message : "Failed to load");
      setLoading(false);
    });
  }, [accessToken]);

  useEffect(() => { void loadData(); }, [loadData]); // eslint-disable-line react-hooks/set-state-in-effect

  const createUser = async () => {
    if (!formUsername.trim() || !formEmail.trim()) return;
    setSaving(true);
    setFormError(null);
    try {
      await apiFetch("/admin/users", {
        method: "POST",
        body: JSON.stringify({ username: formUsername.trim(), email: formEmail.trim(), group: formGroup, tempPassword: formPassword || undefined }),
      });
      setShowForm(false);
      setFormUsername(""); setFormEmail(""); setFormGroup("viewer"); setFormPassword("");
      await loadData();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Failed to create user");
    } finally {
      setSaving(false);
    }
  };

  const deleteUser = async (username: string) => {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
    try {
      await apiFetch(`/admin/users/${encodeURIComponent(username)}`, { method: "DELETE" });
      await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete user");
    }
  };

  const changeGroup = async (username: string, currentGroups: string[], newGroup: GroupName) => {
    try {
      // Remove from all current groups
      for (const g of currentGroups) {
        await apiFetch(`/admin/users/${encodeURIComponent(username)}/groups/${g}`, { method: "DELETE" });
      }
      // Add to new group
      await apiFetch(`/admin/users/${encodeURIComponent(username)}/groups/${newGroup}`, { method: "POST" });
      await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update group");
    }
  };

  const resetDemo = async () => {
    const ok = confirm(
      "Reset demo data? This clears screening history, imported/custom trials, Dev Ops tests, queues, assistant history, and restores default screening rules. Users, roles, patients, and baseline trials are preserved."
    );
    if (!ok) return;
    setResettingDemo(true);
    setError(null);
    setResetSummary(null);
    try {
      const result = await api.resetDemoData();
      setResetSummary(result.summary);
      await loadData();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reset demo data");
    } finally {
      setResettingDemo(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    padding: "8px 12px", borderRadius: "6px", border: "1px solid var(--border)",
    background: "var(--surface2)", color: "var(--text)", fontSize: "13px", outline: "none", width: "100%",
  };
  const btnPrimary: React.CSSProperties = {
    display: "flex", alignItems: "center", gap: "5px", padding: "8px 16px",
    borderRadius: "7px", border: "none", background: "var(--accent)", color: "white",
    fontSize: "12px", fontWeight: 600, cursor: "pointer",
  };
  const btnSecondary: React.CSSProperties = {
    padding: "8px 16px", borderRadius: "7px", border: "1px solid var(--border)",
    background: "none", color: "var(--text2)", fontSize: "12px", cursor: "pointer",
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* Header */}
      <div style={{ padding: "16px 24px", borderBottom: "1px solid var(--border)", background: "var(--surface)", display: "flex", alignItems: "center", gap: "12px", flexShrink: 0 }}>
        <Key size={18} style={{ color: "var(--accent)" }} />
        <h2 style={{ margin: 0, fontSize: "16px", fontWeight: 700 }}>Cognito User Management</h2>
        <span style={{ fontSize: "11px", color: "var(--text2)", marginLeft: "4px" }}>Pool: {import.meta.env.VITE_COGNITO_USER_POOL_ID || "us-east-1_ZyRil52W0"}</span>
        <button onClick={loadData} disabled={loading} style={{ marginLeft: "auto", ...btnSecondary, display: "flex", alignItems: "center", gap: "5px" }}>
          <RefreshCw size={13} style={{ animation: loading ? "spin 1s linear infinite" : undefined }} />
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: "2px", padding: "8px 24px 0", borderBottom: "1px solid var(--border)", background: "var(--surface)", flexShrink: 0 }}>
        {(["users", "groups", "demo"] as Tab[]).map((id) => (
          <button key={id} onClick={() => setActiveTab(id)} style={{
            display: "flex", alignItems: "center", gap: "5px", padding: "9px 14px",
            border: "none", background: "none", color: activeTab === id ? "var(--accent)" : "var(--text2)",
            fontSize: "12px", fontWeight: activeTab === id ? 600 : 500, cursor: "pointer",
            borderBottom: `2px solid ${activeTab === id ? "var(--accent)" : "transparent"}`,
            borderRadius: "6px 6px 0 0",
          }}>
            {id === "users" ? <Users size={13} /> : id === "groups" ? <Shield size={13} /> : <RefreshCw size={13} />}
            {id === "users" ? "Users" : id === "groups" ? "Groups" : "Demo Reset"}
          </button>
        ))}
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
        {error && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", padding: "12px 16px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: "8px", color: "#dc2626", fontSize: "13px", marginBottom: "16px" }}>
            <AlertCircle size={15} />
            {error}
          </div>
        )}

        {/* ─── Users Tab ─────────────────────────────────────────────────────── */}
        {activeTab === "users" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
              <span style={{ fontSize: "12px", color: "var(--text2)" }}>{users.length} users</span>
              <button style={btnPrimary} onClick={() => { setShowForm(true); setFormError(null); }}>
                <UserPlus size={13} /> Add User
              </button>
            </div>

            {showForm && (
              <div style={{ padding: "16px", border: "1px solid var(--border)", borderRadius: "10px", background: "var(--surface)", marginBottom: "16px", display: "flex", flexDirection: "column", gap: "10px" }}>
                <div style={{ fontSize: "13px", fontWeight: 600 }}>New User</div>
                {formError && (
                  <div style={{ fontSize: "12px", color: "#dc2626", background: "#fef2f2", padding: "8px 12px", borderRadius: "6px", border: "1px solid #fecaca" }}>{formError}</div>
                )}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" }}>
                  <input style={inputStyle} placeholder="Username (email)" value={formUsername} onChange={(e) => setFormUsername(e.target.value)} />
                  <input style={inputStyle} placeholder="Email address" value={formEmail} onChange={(e) => setFormEmail(e.target.value)} />
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" }}>
                  <select style={{ ...inputStyle }} value={formGroup} onChange={(e) => setFormGroup(e.target.value as GroupName)}>
                    {GROUPS.map((g) => <option key={g} value={g}>{g.charAt(0).toUpperCase() + g.slice(1)}</option>)}
                  </select>
                  <input style={inputStyle} type="password" placeholder="Temporary password (optional)" value={formPassword} onChange={(e) => setFormPassword(e.target.value)} />
                </div>
                <div style={{ display: "flex", gap: "8px" }}>
                  <button style={btnPrimary} onClick={createUser} disabled={saving}>
                    <Save size={12} /> {saving ? "Creating…" : "Create"}
                  </button>
                  <button style={btnSecondary} onClick={() => { setShowForm(false); setFormError(null); }}>Cancel</button>
                </div>
              </div>
            )}

            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              {users.map((user) => {
                const primaryGroup = (user.groups[0] || "viewer") as GroupName;
                const gc = GROUP_COLORS[primaryGroup] || GROUP_COLORS.viewer;
                return (
                  <div key={user.username} style={{ display: "flex", alignItems: "center", gap: "12px", padding: "12px 16px", border: "1px solid var(--border)", borderRadius: "9px", background: "var(--surface)" }}>
                    <div style={{ width: "34px", height: "34px", borderRadius: "50%", background: gc.bg, border: `1px solid ${gc.border}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "13px", fontWeight: 700, color: gc.color, flexShrink: 0 }}>
                      {user.username[0]?.toUpperCase() || "?"}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: "13px", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user.username}</div>
                      <div style={{ fontSize: "11px", color: "var(--text2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user.email}</div>
                    </div>
                    {/* Role selector */}
                    <select
                      value={primaryGroup}
                      onChange={(e) => changeGroup(user.username, user.groups, e.target.value as GroupName)}
                      style={{ padding: "4px 8px", borderRadius: "6px", border: `1px solid ${gc.border}`, background: gc.bg, color: gc.color, fontSize: "11px", fontWeight: 600, cursor: "pointer" }}
                    >
                      {GROUPS.map((g) => <option key={g} value={g}>{g.charAt(0).toUpperCase() + g.slice(1)}</option>)}
                    </select>
                    {/* Status */}
                    <span style={{ fontSize: "10px", padding: "3px 7px", borderRadius: "4px", background: user.enabled ? "#dcfce7" : "#fef2f2", color: user.enabled ? "#16a34a" : "#ef4444", fontWeight: 600, textTransform: "uppercase", flexShrink: 0 }}>
                      {user.status === "FORCE_CHANGE_PASSWORD" ? "PENDING" : user.enabled ? "ACTIVE" : "DISABLED"}
                    </span>
                    <button onClick={() => deleteUser(user.username)} style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", padding: "4px", flexShrink: 0 }}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                );
              })}
              {!loading && users.length === 0 && (
                <div style={{ textAlign: "center", padding: "40px", color: "var(--text2)", fontSize: "13px" }}>No users found.</div>
              )}
            </div>
          </div>
        )}

        {/* ─── Groups Tab ────────────────────────────────────────────────────── */}
        {activeTab === "groups" && (
          <div>
            <div style={{ marginBottom: "16px" }}>
              <span style={{ fontSize: "12px", color: "var(--text2)" }}>{groups.length} groups — membership is managed from the Users tab</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
              {groups.map((group) => {
                const gc = GROUP_COLORS[group.name as GroupName] || GROUP_COLORS.viewer;
                return (
                  <div key={group.name} style={{ border: "1px solid var(--border)", borderRadius: "10px", background: "var(--surface)", overflow: "hidden" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "12px", padding: "12px 16px", borderBottom: group.members.length > 0 ? "1px solid var(--border)" : undefined }}>
                      <div style={{ width: "28px", height: "28px", borderRadius: "50%", background: gc.bg, border: `1px solid ${gc.border}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Shield size={14} style={{ color: gc.color }} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontSize: "13px", fontWeight: 700 }}>{group.name.charAt(0).toUpperCase() + group.name.slice(1)}</div>
                        <div style={{ fontSize: "11px", color: "var(--text2)" }}>{group.description || (group.name === "admin" ? "Full access — all screens and admin actions" : group.name === "researcher" ? "Screening, analytics, trial builder" : "Read-only screening and analytics")}</div>
                      </div>
                      <span style={{ fontSize: "11px", color: "var(--text2)", background: "var(--surface2)", padding: "3px 8px", borderRadius: "4px", border: "1px solid var(--border)" }}>
                        {group.members.length} {group.members.length === 1 ? "member" : "members"}
                      </span>
                    </div>
                    {group.members.length > 0 && (
                      <div style={{ padding: "10px 16px", display: "flex", gap: "6px", flexWrap: "wrap" }}>
                        {group.members.map((username) => (
                          <span key={username} style={{ display: "flex", alignItems: "center", gap: "4px", padding: "3px 8px", borderRadius: "12px", background: gc.bg, border: `1px solid ${gc.border}`, fontSize: "11px", color: gc.color, fontWeight: 500 }}>
                            {username}
                            <button
                              onClick={() => {
                                const u = users.find((u) => u.username === username);
                                if (u) changeGroup(username, u.groups, "viewer");
                              }}
                              style={{ background: "none", border: "none", color: gc.color, cursor: "pointer", padding: "0 1px", opacity: 0.6, lineHeight: 1 }}
                              title="Remove from group"
                            >
                              <X size={10} />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
              {!loading && groups.length === 0 && (
                <div style={{ textAlign: "center", padding: "40px", color: "var(--text2)", fontSize: "13px" }}>No groups found.</div>
              )}
            </div>

            {/* Add group note */}
            <div style={{ marginTop: "16px", padding: "12px 16px", background: "var(--surface2)", border: "1px solid var(--border)", borderRadius: "8px", fontSize: "12px", color: "var(--text2)", display: "flex", alignItems: "center", gap: "8px" }}>
              <Plus size={13} />
              To add a new group, use the AWS CLI: <code style={{ fontFamily: "monospace", background: "var(--surface)", padding: "2px 6px", borderRadius: "4px", border: "1px solid var(--border)" }}>aws cognito-idp create-group --user-pool-id us-east-1_ZyRil52W0 --group-name &lt;name&gt;</code>
            </div>
          </div>
        )}

        {/* ─── Demo Reset Tab ───────────────────────────────────────────────── */}
        {activeTab === "demo" && (
          <div style={{ maxWidth: "760px", display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ padding: "18px", border: "1px solid var(--border)", borderRadius: "10px", background: "var(--surface)" }}>
              <div style={{ display: "flex", alignItems: "flex-start", gap: "12px" }}>
                <div style={{ width: "34px", height: "34px", borderRadius: "8px", background: "#fef3c7", color: "#92400e", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                  <RefreshCw size={16} />
                </div>
                <div style={{ flex: 1 }}>
                  <h3 style={{ margin: "0 0 6px", fontSize: "15px" }}>Reset demo data</h3>
                  <p style={{ margin: "0 0 12px", fontSize: "13px", color: "var(--text2)", lineHeight: 1.55 }}>
                    Restore the environment to a clean demo-ready baseline. This keeps users, roles, patients, and the two seeded baseline trials, but clears activity and artifacts created during a demo.
                  </p>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginBottom: "14px" }}>
                    {[
                      "Clears screening history and analytics activity",
                      "Deletes imported/custom trials and HealthLake trial resources",
                      "Deletes Dev Ops test cases",
                      "Restores default screening rules",
                      "Purges pending screening/escalation queues",
                      "Clears assistant conversation history",
                    ].map((item) => (
                      <div key={item} style={{ fontSize: "12px", color: "var(--text2)", padding: "8px 10px", border: "1px solid var(--border)", borderRadius: "7px", background: "var(--surface2)" }}>
                        {item}
                      </div>
                    ))}
                  </div>
                  <button
                    onClick={resetDemo}
                    disabled={resettingDemo}
                    style={{ ...btnPrimary, background: "#dc2626", opacity: resettingDemo ? 0.7 : 1 }}
                  >
                    <RefreshCw size={13} style={{ animation: resettingDemo ? "spin 1s linear infinite" : undefined }} />
                    {resettingDemo ? "Resetting…" : "Reset Demo Data"}
                  </button>
                </div>
              </div>
            </div>

            {resetSummary && (
              <div style={{ padding: "16px", border: "1px solid #bbf7d0", borderRadius: "10px", background: "#f0fdf4", color: "#166534" }}>
                <div style={{ fontSize: "13px", fontWeight: 700, marginBottom: "8px" }}>Demo reset complete</div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px", fontSize: "12px" }}>
                  <div>Screening records deleted: {resetSummary.screening_history_deleted}</div>
                  <div>Trials deleted: {resetSummary.trials_deleted}</div>
                  <div>Questionnaires deleted: {resetSummary.questionnaires_deleted}</div>
                  <div>Responses deleted: {resetSummary.questionnaire_responses_deleted}</div>
                  <div>Research studies deleted: {resetSummary.research_studies_deleted}</div>
                  <div>Dev Ops tests deleted: {resetSummary.devops_tests_deleted}</div>
                  <div>Rules restored: {resetSummary.screening_rules_restored}</div>
                  <div>Assistant sessions cleared: {resetSummary.assistant_sessions_cleared}</div>
                  <div>Queues purged: {resetSummary.queues_purged.length ? resetSummary.queues_purged.join(", ") : "none"}</div>
                  <div>Baseline trials kept: {resetSummary.preserved_trials.join(", ")}</div>
                </div>
                {resetSummary.warnings.length > 0 && (
                  <div style={{ marginTop: "10px", color: "#92400e" }}>
                    Warnings: {resetSummary.warnings.join("; ")}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
