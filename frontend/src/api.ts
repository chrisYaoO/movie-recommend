export async function api<T = void>(path: string, init?: RequestInit): Promise<T> {
  await window.moviesDesktop?.waitForBackend();
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: requestHeaders(init)
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, contentType));
  }

  if (!contentType) {
    return undefined as T;
  }
  if (!contentType.includes("application/json")) {
    throw new Error(`Expected JSON from ${path}, got ${contentType}`);
  }
  return response.json() as Promise<T>;
}

export function errorMessage(error: unknown) {
  if (error instanceof DOMException && error.name === "AbortError") return "Request canceled";
  return error instanceof Error ? error.message : "Request failed";
}

export function isErrorStatus(value: string) {
  return /^Could not\b/.test(value) || /\bfailed\b/i.test(value) || /^\d{3}\b/.test(value);
}

function apiUrl(path: string) {
  if (/^https?:\/\//.test(path)) return path;
  const baseUrl = window.moviesDesktop?.apiBaseUrl || import.meta.env.VITE_API_BASE_URL || "";
  return baseUrl ? `${baseUrl}${path}` : path;
}

function requestHeaders(init?: RequestInit) {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return headers;
}

async function responseErrorMessage(response: Response, contentType: string) {
  if (contentType.includes("application/json")) {
    const body = await response.json().catch(() => null);
    if (body && typeof body === "object") {
      const detail = "detail" in body ? body.detail : null;
      const message = "message" in body ? body.message : null;
      if (typeof detail === "string") return detail;
      if (typeof message === "string") return message;
    }
  }
  const text = await response.text().catch(() => "");
  return text.trim() || `${response.status} ${response.statusText}`;
}
