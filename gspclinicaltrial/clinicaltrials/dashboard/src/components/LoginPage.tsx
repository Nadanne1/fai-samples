import { useState } from "react";
import { signIn } from "../services/AuthService";
import type { Session } from "../services/AuthService";
import { FlaskConical, Shield, Activity, Users, BarChart3, Workflow } from "lucide-react";
import styles from "./LoginPage.module.css";

const FEATURES = [
  {
    icon: <Users size={18} />,
    title: "Screening",
    desc: "Import trial criteria, select a patient, and produce a documented match decision with clear rationale.",
  },
  {
    icon: <BarChart3 size={18} />,
    title: "Analytics",
    desc: "Track screening volume, eligible candidates, borderline cases, and trial-level recruitment performance.",
  },
  {
    icon: <FlaskConical size={18} />,
    title: "Trial Builder",
    desc: "Manage the trial library, review imported eligibility criteria, and prepare studies for screening.",
  },
  {
    icon: <Workflow size={18} />,
    title: "Flow Editor",
    desc: "Model and refine the screening workflow so teams can align process steps before operational rollout.",
  },
  {
    icon: <Activity size={18} />,
    title: "Dev Ops",
    desc: "Validate flows, run tests, review traces, and keep the demo environment ready for stakeholders.",
  },
  {
    icon: <Shield size={18} />,
    title: "Admin",
    desc: "Manage user access and roles so researchers, reviewers, and administrators see the right capabilities.",
  },
];

export default function LoginPage({ onLogin }: { onLogin: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const s = await signIn(email.trim(), password);
      onLogin(s);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.page}>
      {/* Left panel */}
      <div className={styles.left}>
        <div className={styles.brand}>
          <div className={styles.logoBox}>
            <FlaskConical size={20} />
          </div>
          <span className={styles.brandName}>TrialMatch360</span>
        </div>

        <div className={styles.hero}>
          <h1 className={styles.heroTitle}>
            Accelerate patient-to-trial<br />matching.
          </h1>
          <p className={styles.heroSub}>
            A business workflow for importing studies, screening potential candidates,
            explaining eligibility decisions, and tracking recruitment performance across
            the trial portfolio.
          </p>

          <div className={styles.features}>
            {FEATURES.map((f) => (
              <div key={f.title} className={styles.feature}>
                <div className={styles.featureIcon}>{f.icon}</div>
                <div>
                  <p className={styles.featureTitle}>{f.title}</p>
                  <p className={styles.featureDesc}>{f.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className={styles.leftFooter}>
          <Shield size={13} />
          Secure access &nbsp;·&nbsp; auditable screening decisions &nbsp;·&nbsp; role-based operations
        </div>
      </div>

      {/* Right panel — form */}
      <div className={styles.right}>
        <div className={styles.formWrap}>
          {/* Mobile logo */}
          <div className={styles.mobileBrand}>
            <div className={styles.logoBox}>
              <FlaskConical size={16} />
            </div>
            <span className={styles.brandName}>TrialMatch360</span>
          </div>

          <div className={styles.card}>
            <h2 className={styles.cardTitle}>Sign in</h2>
            <p className={styles.cardSub}>Access requires an authorized account.</p>

            {error && (
              <div className={styles.errorBox}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className={styles.form}>
              <div className={styles.field}>
                <label htmlFor="email" className={styles.label}>Email address</label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={styles.input}
                  placeholder="you@example.com"
                />
              </div>

              <div className={styles.field}>
                <label htmlFor="password" className={styles.label}>Password</label>
                <input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={styles.input}
                  placeholder="••••••••"
                />
              </div>

              <button type="submit" disabled={loading} className={styles.submitBtn}>
                {loading ? (
                  <>
                    <svg className={styles.spinner} viewBox="0 0 24 24" fill="none">
                      <circle className={styles.spinnerTrack} cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className={styles.spinnerArc} fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    Signing in…
                  </>
                ) : (
                  "Sign in"
                )}
              </button>
            </form>
          </div>

          <p className={styles.footerNote}>
            &copy; {new Date().getFullYear()} TrialMatch360. All rights reserved.
          </p>
        </div>
      </div>
    </div>
  );
}
