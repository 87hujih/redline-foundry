import { z } from "zod";
import type {
  Approval,
  Citation,
  Conversation,
  Resource,
  ResourceVersion,
  RunDetail,
  RunSummary,
  Session,
  UploadCapabilities,
  UploadResult,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "/api";

const errorSchema = z.object({ error: z.string() });

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function parseError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("X-Request-ID") || undefined;
  try {
    const parsed = errorSchema.safeParse(await response.json());
    if (parsed.success) return new ApiError(parsed.data.error, response.status, requestId);
  } catch {
    // Fall through to the stable public error.
  }
  return new ApiError("服务暂时不可用，请稍后重试", response.status, requestId);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const result = params.toString();
  return result ? `?${result}` : "";
}

export const api = {
  listSessions: async () => (await request<{ sessions: Session[] }>("/assistant/sessions")).sessions,
  getSession: (id: string) => request<Conversation>(`/assistant/sessions/${id}`),
  deleteSession: (id: string) => request<void>(`/assistant/sessions/${id}`, { method: "DELETE" }),
  getSelection: (id: string) => request<{ resource_id: string | null }>(`/assistant/sessions/${id}/resource-selection`),
  setSelection: (id: string, resourceId: string) => request<{ resource_id: string }>(`/assistant/sessions/${id}/resource-selection`, { method: "PUT", body: JSON.stringify({ resource_id: resourceId }) }),
  getCapabilities: async () => (await request<{ upload: UploadCapabilities }>("/assistant/capabilities")).upload,
  uploadFile: async (file: File, sessionId?: string): Promise<UploadResult> => {
    const form = new FormData();
    form.set("file", file);
    const path = sessionId ? `/assistant/sessions/${sessionId}/files` : "/assistant/conversations/files";
    return request<UploadResult>(path, { method: "POST", body: form });
  },
  createConversation: (message: string, resourceId: string, requestId: string) => request<Conversation>("/assistant/conversations", {
    method: "POST",
    headers: { "X-Request-ID": requestId },
    body: JSON.stringify({ message, resource_id: resourceId }),
  }),

  listResources: async () => (await request<{ resources: Resource[] }>("/resources")).resources,
  getResource: (id: string) => request<{ resource: Resource; current_version: ResourceVersion | null }>(`/resources/${id}`),
  deleteResource: (id: string) => request<void>(`/resources/${id}`, { method: "DELETE" }),
  searchResource: (id: string, query: string) => request<{ query: string; citations: Citation[] }>(`/resources/${id}/search${queryString({ q: query })}`),
  exportResource: (id: string) => download(`${API_BASE}/resources/${id}/export`),

  listRuns: async (filters: { status?: string; resource_id?: string; limit?: number }) => (await request<{ runs: RunSummary[] }>(`/agent/runs${queryString(filters)}`)).runs,
  getRun: (id: string) => request<RunDetail>(`/agent/runs/${id}`),

  listApprovals: async (filters: { status?: string; limit?: number }) => (await request<{ approvals: Approval[] }>(`/agent/approvals${queryString(filters)}`)).approvals,
  getApproval: async (id: string) => (await request<{ approval: Approval }>(`/agent/approvals/${id}`)).approval,
  decideApproval: (id: string, decision: "approve" | "reject", reason: string) => request<{ approval: { id: string; status: string } }>(`/agent/approvals/${id}/${decision}`, { method: "POST", body: JSON.stringify({ reason }) }),
  downloadFile: (id: string) => download(`${API_BASE}/files/${id}/download`),
};

async function download(url: string): Promise<void> {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = /filename\*=utf-8''([^;]+)/i.exec(disposition)?.[1];
  const quoted = /filename="([^"]+)"/i.exec(disposition)?.[1];
  const plain = /filename=([^;]+)/i.exec(disposition)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : quoted || plain || "download";
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(objectUrl);
}

export { API_BASE, parseError };
