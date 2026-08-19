/**
 * AuthService — Cognito USER_PASSWORD_AUTH via direct API (no Amplify).
 * Role comes from cognito:groups claim in the ID token.
 */

const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID as string;
const REGION = (import.meta.env.VITE_COGNITO_REGION as string) ?? "us-east-1";
const COGNITO_URL = `https://cognito-idp.${REGION}.amazonaws.com/`;

export type Role = "admin" | "researcher" | "viewer";

export interface Session {
  idToken: string;
  accessToken: string;
  refreshToken: string;
  email: string;
  displayName: string;
  role: Role;
}

function parseJwt(token: string): Record<string, unknown> {
  try {
    const part = token.split(".")[1];
    const padded = part + "==".slice(0, (4 - (part.length % 4)) % 4);
    return JSON.parse(atob(padded));
  } catch {
    return {};
  }
}

function extractRole(payload: Record<string, unknown>): Role {
  const groups = payload["cognito:groups"];
  if (Array.isArray(groups)) {
    if (groups.includes("admin")) return "admin";
    if (groups.includes("researcher")) return "researcher";
  }
  return "viewer";
}

export async function signIn(email: string, password: string): Promise<Session> {
  const res = await fetch(COGNITO_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
    },
    body: JSON.stringify({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: CLIENT_ID,
      AuthParameters: { USERNAME: email, PASSWORD: password },
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.message || err.__type || "Login failed");
  }

  const data = await res.json();
  const idToken = data.AuthenticationResult?.IdToken as string;
  const accessToken = data.AuthenticationResult?.AccessToken as string;
  const refreshToken = data.AuthenticationResult?.RefreshToken as string;
  if (!idToken) throw new Error("Authentication failed. Please try again.");

  const payload = parseJwt(idToken);
  const givenName = (payload["given_name"] as string) || "";
  const familyName = (payload["family_name"] as string) || "";
  const emailClaim = (payload["email"] as string) || email;
  const displayName =
    givenName && familyName
      ? `${givenName} ${familyName}`
      : emailClaim.split("@")[0].replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  const session: Session = {
    idToken,
    accessToken,
    refreshToken,
    email: emailClaim,
    displayName,
    role: extractRole(payload),
  };
  sessionStorage.setItem("cts_session", JSON.stringify(session));
  return session;
}

export async function refreshSession(): Promise<Session | null> {
  const raw = sessionStorage.getItem("cts_session");
  if (!raw) return null;
  try {
    const s = JSON.parse(raw) as Session;
    if (!s.refreshToken) return null;
    const res = await fetch(COGNITO_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
      },
      body: JSON.stringify({
        AuthFlow: "REFRESH_TOKEN_AUTH",
        ClientId: CLIENT_ID,
        AuthParameters: { REFRESH_TOKEN: s.refreshToken },
      }),
    });
    if (!res.ok) {
      sessionStorage.removeItem("cts_session");
      return null;
    }
    const data = await res.json();
    const idToken = data.AuthenticationResult?.IdToken as string;
    const accessToken = (data.AuthenticationResult?.AccessToken as string) || s.accessToken;
    if (!idToken) return null;
    const payload = parseJwt(idToken);
    const updated: Session = { ...s, idToken, accessToken, role: extractRole(payload) };
    sessionStorage.setItem("cts_session", JSON.stringify(updated));
    return updated;
  } catch {
    return null;
  }
}

export function getSession(): Session | null {
  const raw = sessionStorage.getItem("cts_session");
  if (!raw) return null;
  try {
    const s = JSON.parse(raw) as Session;
    const payload = parseJwt(s.idToken);
    const exp = payload.exp;
    if (typeof exp !== "number" || exp * 1000 < Date.now()) {
      sessionStorage.removeItem("cts_session");
      return null;
    }
    // Migrate sessions from before accessToken was added — sign them out so they
    // re-authenticate and obtain a fresh token with all fields populated.
    if (!s.accessToken) {
      sessionStorage.removeItem("cts_session");
      return null;
    }
    return s;
  } catch {
    sessionStorage.removeItem("cts_session");
    return null;
  }
}

export function signOut() {
  sessionStorage.removeItem("cts_session");
}

export function canAdmin(role: Role) {
  return role === "admin";
}

export function canWrite(role: Role) {
  return role === "admin" || role === "researcher";
}
