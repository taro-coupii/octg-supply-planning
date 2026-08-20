import { clearToken, getToken } from "./auth";

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra };
}

// On a 401 response, clear the (now-invalid) token and send the user to
// /login?next=<current path>. Guarded so the /auth/login call itself never
// loops back into a redirect.
function handleUnauthorized(path: string): void {
  if (path.startsWith("/auth/login")) return;
  clearToken();
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/login?next=${next}`;
}

export async function apiGet<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, { headers: authHeaders({ Accept: "application/json" }) });
  if (resp.status === 401) {
    handleUnauthorized(path);
  }
  if (!resp.ok) {
    throw new Error(`GET ${path} failed: ${resp.status}`);
  }
  return (await resp.json()) as T;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function detailToMessage(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in (body as Record<string, unknown>)) {
    const detail = (body as Record<string, unknown>).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return JSON.stringify(detail);
  }
  return `request failed: ${status}`;
}

// apiSend: POST/PATCH/PUT/DELETE with a JSON body. On non-ok responses the
// error body (e.g. FastAPI's {detail: ...}) is captured on ApiError.body so
// callers can render per-row 422 detail lists instead of a generic message.
export async function apiSend<T>(
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown
): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method,
    headers: authHeaders(
      body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" }
    ),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    handleUnauthorized(path);
  }
  let parsed: unknown = null;
  const text = await resp.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!resp.ok) {
    throw new ApiError(detailToMessage(resp.status, parsed), resp.status, parsed);
  }
  return parsed as T;
}

// apiUpload: multipart/form-data POST (file upload). Same error-capture
// convention as apiSend.
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, { method: "POST", headers: authHeaders(), body: form });
  if (resp.status === 401) {
    handleUnauthorized(path);
  }
  let parsed: unknown = null;
  const text = await resp.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!resp.ok) {
    throw new ApiError(detailToMessage(resp.status, parsed), resp.status, parsed);
  }
  return parsed as T;
}

// apiDownload: GET a binary (xlsx) response with the Authorization header,
// then trigger a browser download via a blob objectURL (spec 裁定AU-1 — no
// raw <a href> to API paths, since those can't carry the Bearer header).
export async function apiDownload(path: string, filename: string): Promise<void> {
  const resp = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  if (resp.status === 401) {
    handleUnauthorized(path);
  }
  if (!resp.ok) {
    throw new Error(`GET ${path} failed: ${resp.status}`);
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
