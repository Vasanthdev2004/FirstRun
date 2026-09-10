"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionView } from "./api-types";

export type Session = SessionView;

export class ApiError extends Error {
  constructor(public readonly status: number) {
    super(status === 401 ? "Your session has ended. Sign in again to continue."
      : status === 403 ? "Your account does not have permission for this action."
      : status === 404 ? "This repository or case is not available to your account."
      : status === 409 ? "The case changed while you were reviewing it. Refresh before trying again."
      : status === 429 ? "Too many requests. Wait a moment, then try again."
      : "FirstRun could not complete this request. Check the API connection and try again.");
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function mutate<T>(path: string, body: unknown, csrfToken: string | null): Promise<T> {
  if (!csrfToken) return Promise.reject(new ApiError(401));
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
  });
}

export function useResource<T>(path: string | null, pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const loadedPath = useRef<string | null>(null);

  useEffect(() => {
    if (!path) { setData(null); setLoading(false); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (loadedPath.current !== path) { setData(null); setLoading(true); }
    async function read() {
      try {
        const result = await api<T>(path!, { signal: controller.signal });
        if (!controller.signal.aborted) {
          loadedPath.current = path;
          setData(result); setError(null);
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          // Permission loss must remove previously visible protected data.
          if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) setData(null);
          setError(cause instanceof ApiError ? cause : new Error("The FirstRun API is not reachable. Start the API, then try again."));
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (pollMs) timer = setTimeout(read, pollMs);
        }
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, pollMs, revision]);

  return { data, error, loading, refresh };
}
