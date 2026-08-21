import { afterEach, describe, expect, it, vi } from "vitest";
import { SSEParser, streamTurn } from "./sse";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SSEParser", () => {
  it("parses frames split across network chunks", () => {
    const parser = new SSEParser();
    expect(parser.push('id: 1\nevent: turn_state\ndata: {"sta')).toEqual([]);
    expect(parser.push('tus":"running"}\n\nid: 2\nevent: done\ndata: {}\n\n')).toEqual([
      { id: 1, event: "turn_state", data: { status: "running" } },
      { id: 2, event: "done", data: {} },
    ]);
  });

  it("ignores malformed data without losing the next frame", () => {
    const parser = new SSEParser();
    expect(parser.push("id: 1\nevent: bad\ndata: nope\n\nid: 2\nevent: done\ndata: {}\n\n")).toEqual([
      { id: 2, event: "done", data: {} },
    ]);
  });

  it("supports comments, CRLF, and multiline JSON data", () => {
    const parser = new SSEParser();
    expect(parser.push(': heartbeat\r\nid: 7\r\nevent: message_delta\r\ndata: {"delta":\r\ndata: "risk"}\r\n\r\n')).toEqual([
      { id: 7, event: "message_delta", data: { delta: "risk" } },
    ]);
  });
});

describe("streamTurn", () => {
  it("resumes with the same request contract and suppresses replayed non-terminal events", async () => {
    const body = [
      'id: 4\nevent: message_delta\ndata: {"delta":"replayed"}\n\n',
      'id: 5\nevent: message_delta\ndata: {"delta":"new"}\n\n',
      'id: 5\nevent: error\ndata: {"error":"stopped"}\n\n',
      'id: 5\nevent: done\ndata: {}\n\n',
    ].join("");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const frames: unknown[] = [];

    const cursor = await streamTurn({
      sessionId: "session-1",
      message: "Review termination clauses",
      resourceId: "resource-1",
      requestId: "request-stable",
      afterId: 4,
      onFrame: (frame) => frames.push(frame),
    });

    expect(cursor).toBe(5);
    expect(frames).toEqual([
      { id: 5, event: "message_delta", data: { delta: "new" } },
      { id: 5, event: "error", data: { error: "stopped" } },
      { id: 5, event: "done", data: {} },
    ]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/assistant/sessions/session-1/messages/stream");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ message: "Review termination clauses", resource_id: "resource-1" }),
    });
    const headers = new Headers(init.headers);
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-Request-ID")).toBe("request-stable");
    expect(headers.get("Last-Event-ID")).toBe("4");
  });
});
