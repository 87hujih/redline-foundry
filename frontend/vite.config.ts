import { createHmac, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

const workspaceRoot = fileURLToPath(new URL("..", import.meta.url));

function apiProxy(mode: string): ProxyOptions {
  const env = loadEnv(mode, workspaceRoot, "");
  const secret = env.AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET;
  const principalId = env.DOCREVIEW_FIXED_PRINCIPAL_ID || "33333333-3333-4333-8333-333333333333";
  const organizationId = env.DOCREVIEW_FIXED_ORGANIZATION_ID || "11111111-1111-4111-8111-111111111111";
  const workspaceId = env.DOCREVIEW_FIXED_WORKSPACE_ID || "22222222-2222-4222-8222-222222222222";
  const roles = env.DOCREVIEW_FIXED_ROLES || "owner";

  return {
    target: "http://127.0.0.1:8080",
    changeOrigin: true,
    configure(proxy) {
      proxy.on("proxyReq", (proxyRequest, request) => {
        if (!secret) return;
        const requestIdHeader = request.headers["x-request-id"];
        const requestId = typeof requestIdHeader === "string" && requestIdHeader.trim()
          ? requestIdHeader.trim()
          : randomUUID();
        const requestPath = new URL(request.url || "/", "http://localhost").pathname;
        const issuedAt = new Date().toISOString();
        const canonical = [
          "v1",
          requestId,
          (request.method || "GET").toUpperCase(),
          requestPath,
          "user",
          principalId,
          organizationId,
          workspaceId,
          issuedAt,
          roles,
        ].join("\n");

        proxyRequest.setHeader("X-Request-ID", requestId);
        proxyRequest.setHeader("X-DocReview-Principal-Type", "user");
        proxyRequest.setHeader("X-DocReview-Principal-ID", principalId);
        proxyRequest.setHeader("X-DocReview-Organization-ID", organizationId);
        proxyRequest.setHeader("X-DocReview-Workspace-ID", workspaceId);
        proxyRequest.setHeader("X-DocReview-Identity-Issued-At", issuedAt);
        proxyRequest.setHeader("X-DocReview-Roles", roles);
        proxyRequest.setHeader(
          "X-DocReview-Identity-Signature",
          createHmac("sha256", secret).update(canonical).digest("hex"),
        );
      });
    },
  };
}

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": apiProxy(mode),
      "/healthz": "http://127.0.0.1:8080",
    },
  },
}));
