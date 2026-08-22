/**
 * Client-side session: token storage, the authenticated fetch wrapper, and the
 * login/logout calls.
 *
 * The server keeps no session state (tokens are self-expiring — see
 * backend app/auth/tokens.py), so "logged in" here means exactly "we hold a
 * token that has not yet been rejected". Any 401 from any API call is the
 * server telling us that stopped being true (expiry, restart with a new
 * AUTH_SECRET, deactivated user), and the ONLY correct reaction is the same
 * one: drop the session and land on /login. Centralising that in `authFetch`
 * is what keeps client.ts's ~90 call sites free of per-call 401 handling.
 */

// Deliberately duplicated from client.ts rather than imported: client.ts
// imports authFetch from this module, and an import back the other way would
// make the two modules circular.
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

const TOKEN_KEY = "octg.auth.token";
const USER_KEY = "octg.auth.user";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "planner";
  business_unit_id: string | null;
  business_unit_name: string | null;
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

/** The user as of login. A stale copy is display-only — every ACCESS decision
 * is the server's, so we never re-derive permissions from this object. */
export function getUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b) => b?.detail)
      .catch(() => null);
    throw new Error(
      typeof detail === "string" ? detail : `Login failed (${res.status})`
    );
  }
  const body = (await res.json()) as { token: string; user: AuthUser };
  localStorage.setItem(TOKEN_KEY, body.token);
  localStorage.setItem(USER_KEY, JSON.stringify(body.user));
  return body.user;
}

export function logout(): void {
  // No server call on purpose: there is no server session to destroy.
  clearSession();
  window.location.assign("/login");
}

/**
 * fetch + Authorization header + the single 401 policy described above.
 * client.ts uses this for every API call.
 */
export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(input, { ...init, headers });
  if (res.status === 401) {
    clearSession();
    window.location.assign("/login");
    // The page is navigating away; reject so no caller renders from a 401.
    throw new Error("Session expired — redirecting to login.");
  }
  return res;
}

/**
 * Append the session token to a download URL (<a href> browser navigations
 * cannot carry an Authorization header — backend MVP-COMPROMISE[C-14]).
 * Undefined-in, undefined-out so `href={url && withToken(url)}` stays simple.
 */
export function withToken(url: string): string;
export function withToken(url: undefined): undefined;
export function withToken(url: string | undefined): string | undefined;
export function withToken(url: string | undefined): string | undefined {
  const token = getToken();
  if (!url || !token) return url;
  return url + (url.includes("?") ? "&" : "?") + "access_token=" + encodeURIComponent(token);
}
