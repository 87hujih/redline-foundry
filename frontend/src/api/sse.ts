import { API_BASE, parseError } from "./client";
import type { JsonValue, SSEFrame } from "./types";

export class SSEParser {
  private buffer = "";

  push(chunk: string): SSEFrame[] {
    this.buffer += chunk.replaceAll("\r\n", "\n");
    const frames: SSEFrame[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }
}

function parseFrame(raw: string): SSEFrame | null {
  let id: number | undefined;
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "id" && /^\d+$/.test(value)) id = Number(value);
    if (field === "event" && value && !/[\r\n]/.test(value)) event = value;
    if (field === "data") data.push(value);
  }
  if (id === undefined || data.length === 0) return null;
  try {
    const value = JSON.parse(data.join("\n")) as JsonValue;
    if (!value || Array.isArray(value) || typeof value !== "object") return null;
    return { id, event, data: value as Record<string, JsonValue> };
  } catch {
    return null;
  }
}

export interface StreamTurnOptions {
  sessionId?: string;
  message: string;
  resourceId: string;
  requestId: string;
  afterId?: number;
  signal?: AbortSignal;
  onFrame: (frame: SSEFrame) => void;
}

export async function streamTurn(options: StreamTurnOptions): Promise<number> {
  const path = options.sessionId
    ? `/assistant/sessions/${options.sessionId}/messages/stream`
    : "/assistant/conversations/stream";
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "X-Request-ID": options.requestId,
  };
  if (options.afterId !== undefined && options.afterId > 0) headers["Last-Event-ID"] = String(options.afterId);
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify({ message: options.message, resource_id: options.resourceId }),
    signal: options.signal,
  });
  if (!response.ok) throw await parseError(response);
  if (!response.body) throw new Error("当前浏览器不支持流式响应");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  let cursor = options.afterId || 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        if (frame.id < cursor || (frame.id === cursor && frame.event !== "done" && frame.event !== "error")) continue;
        cursor = Math.max(cursor, frame.id);
        options.onFrame(frame);
      }
    }
  } finally {
    reader.releaseLock();
  }
  return cursor;
}

export function newRequestId(): string {
  return crypto.randomUUID();
}
