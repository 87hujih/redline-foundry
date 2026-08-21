import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, parseError } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("API client contract", () => {
  it("deletes a resource through the same-origin API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.deleteResource("resource-1")).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/resources/resource-1");
    expect(init).toMatchObject({ method: "DELETE", credentials: "same-origin" });
  });

  it("sends JSON writes to the same-origin API with stable headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ resource_id: "resource-2" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.setSelection("session-1", "resource-2")).resolves.toEqual({ resource_id: "resource-2" });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/assistant/sessions/session-1/resource-selection");
    expect(init).toMatchObject({
      method: "PUT",
      credentials: "same-origin",
      body: JSON.stringify({ resource_id: "resource-2" }),
    });
    const headers = new Headers(init.headers);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("keeps multipart uploads browser-owned and uses the existing-session endpoint", async () => {
    const result = {
      session: { id: "session-1", title: "Document", web_search_enabled: false, last_message_at: "2026-08-20T10:30:00Z", created_at: "2026-08-20T10:30:00Z", updated_at: "2026-08-20T10:30:00Z" },
      messages: [],
      resource: null,
      error_message: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(result), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.uploadFile(new File(["contract"], "contract.md", { type: "text/markdown" }), "session-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/assistant/sessions/session-1/files");
    expect(init.body).toBeInstanceOf(FormData);
    expect(new Headers(init.headers).has("Content-Type")).toBe(false);
  });

  it("maps the public error envelope and request ID to ApiError", async () => {
    const response = new Response(JSON.stringify({ error: "资源不可用" }), {
      status: 404,
      headers: { "Content-Type": "application/json", "X-Request-ID": "request-123" },
    });

    await expect(parseError(response)).resolves.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: "资源不可用",
        status: 404,
        requestId: "request-123",
      }),
    );
  });

  it("keeps the caller request ID on a durable conversation retry", async () => {
    const conversation = {
      session: { id: "session-1", title: "Review", web_search_enabled: false, last_message_at: "2026-08-20T10:30:00Z", created_at: "2026-08-20T10:30:00Z", updated_at: "2026-08-20T10:30:00Z" },
      messages: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(conversation), { status: 201, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.createConversation("Review this", "resource-1", "stable-request-id");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Request-ID")).toBe("stable-request-id");
  });

  it("does not expose an unstructured upstream error body", async () => {
    const response = new Response("proxy failure details", { status: 502 });

    await expect(parseError(response)).resolves.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: "服务暂时不可用，请稍后重试",
        status: 502,
      }),
    );
  });
});
