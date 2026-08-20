// Client-side auth state (spec §フロントエンド). Token + last-known user are
// cached in localStorage; there is no server-side session (logout is
// client-only — spec §適用 "logout エンドポイントは作らない").

const TOKEN_KEY = "octg_token";
const USER_KEY = "octg_user";

export type AuthUser = {
  email: string;
  role: string;
  business_unit_id: string | null;
};

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function getUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function setUser(user: AuthUser): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}
