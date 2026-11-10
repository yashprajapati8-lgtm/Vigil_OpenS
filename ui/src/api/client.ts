// Thin fetch wrappers. Every route goes through the same origin: in production
// nginx proxies /api to the engine, /chaos to the chaos controller and
// /api/store to the payment service; `npm run dev` mirrors that in vite.config.ts.

import { REPLAY } from "../replay/player";
import { replayRequest } from "../replay/api";
import { ApiError } from "./errors";

export { ApiError };

const TOKEN_KEY = "vigil.operatorToken";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: the token lives only for this page view */
  }
}

async function request<T>(method: string, url: string, body?: unknown, auth = false): Promise<T> {
  if (REPLAY) return replayRequest<T>(method, url, body);
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers["X-Vigil-Token"] = token;
  }
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is the stack running?");
  }
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? String((data as { detail: unknown }).detail) : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  get: <T,>(url: string) => request<T>("GET", url),
  post: <T,>(url: string, body?: unknown, auth = false) => request<T>("POST", url, body, auth),
};
