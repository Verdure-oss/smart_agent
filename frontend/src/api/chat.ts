import type {
  ChatRequest,
  ChatResponse,
  HealthResponse,
  HistoryResponse,
} from "../types/api";

const rawApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim() ?? "";
const apiBaseUrl = rawApiBaseUrl.replace(/\/$/, "");

function buildUrl(path: string): string {
  return apiBaseUrl ? `${apiBaseUrl}${path}` : path;
}

function getErrorMessage(payload: unknown, status: number): string {
  if (typeof payload === "object" && payload !== null) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (typeof item === "string" ? item : JSON.stringify(item)))
        .join("；");
    }
  }

  return `请求失败，状态码 ${status}`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(buildUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  const payload = (await response.json().catch(() => null)) as unknown;

  if (!response.ok) {
    throw new Error(getErrorMessage(payload, response.status));
  }

  return payload as T;
}

export async function checkHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", { method: "GET" });
}

export async function sendChatMessage(request: ChatRequest): Promise<ChatResponse> {
  return requestJson<ChatResponse>("/api/chat", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function fetchHistory(sessionId: string): Promise<HistoryResponse> {
  return requestJson<HistoryResponse>(`/api/history/${sessionId}`, {
    method: "GET",
  });
}
