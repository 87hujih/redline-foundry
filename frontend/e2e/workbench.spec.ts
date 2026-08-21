import { expect, test, type Page } from "@playwright/test";

const ids = {
  session: "66666666-6666-4666-8666-666666666666",
  resource: "55555555-5555-4555-8555-555555555555",
  run: "77777777-7777-4777-8777-777777777777",
  step: "88888888-8888-4888-8888-888888888888",
  approval: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const now = "2026-08-20T10:30:00Z";
const session = { id: ids.session, title: "供应商框架协议审阅", web_search_enabled: false, last_message_at: now, created_at: now, updated_at: now };
const resource = { id: ids.resource, title: "2026 供应商框架协议（修订稿）", source_type: "upload", created_at: now };
const messages = [
  { id: "11111111-1111-4111-8111-111111111111", role: "user", kind: "text", payload: { content: "请检查终止条款和责任上限。" }, sequence_no: 1, created_at: now },
  { id: "22222222-2222-4222-8222-222222222222", role: "assistant", kind: "text", payload: { content: "## 初步结论\n\n发现 **2 处高风险条款**：\n\n1. 供应商可单方面即时终止。\n2. 责任上限未排除数据泄露。\n\n建议进入逐条修订。" }, sequence_no: 2, created_at: now },
  { id: "33333333-3333-4333-8333-333333333333", role: "assistant", kind: "session_file", payload: { filename: "一份名称特别长用于验证文件名不会撑破消息区域的供应商数据处理与跨境传输补充协议最终修订版本.docx", file_id: "file-long-name" }, sequence_no: 3, created_at: now },
  ...Array.from({ length: 24 }, (_, index) => ({
    id: `history-${index}`,
    role: index % 2 === 0 ? "user" : "assistant",
    kind: "text",
    payload: { content: `历史审阅消息 ${index + 1}：请继续核对协议中的定义、期限、违约责任和数据处理要求。` },
    sequence_no: index + 4,
    created_at: now,
  })),
];

async function installApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body), headers: { "X-Request-ID": "mock-request" } });
    if (path === "/api/assistant/sessions") return json({ sessions: [session] });
    if (path === `/api/assistant/sessions/${ids.session}` && request.method() === "GET") return json({ session, messages });
    if (path.endsWith("/resource-selection") && request.method() === "GET") return json({ resource_id: ids.resource });
    if (path.endsWith("/resource-selection") && request.method() === "PUT") return json({ resource_id: ids.resource });
    if (path === "/api/assistant/capabilities") return json({ upload: { supported_extensions: [".pdf", ".docx", ".md"], accept: ".pdf,.docx,.md", hint: "支持 pdf、docx、md" } });
    if (path === "/api/resources") return json({ resources: [resource, { ...resource, id: "44444444-4444-4444-8444-444444444444", title: "数据处理补充协议" }] });
    if (path === `/api/resources/${ids.resource}` && request.method() === "DELETE") return route.fulfill({ status: 204 });
    if (path === `/api/resources/${ids.resource}` && request.method() === "GET") return json({ resource, current_version: { id: "version-1", version_number: 3, source: "upload", created_at: now, content: "# 供应商框架协议\n\n## 第八条 终止\n\n供应商可在任何时间书面通知后立即终止本协议。\n\n## 第十二条 责任限制\n\n任一方累计责任不超过过去十二个月已支付费用。" } });
    if (path === `/api/resources/${ids.resource}/export`) return route.fulfill({ status: 200, contentType: "text/markdown", body: "# exported", headers: { "Content-Disposition": "attachment; filename=review.md" } });
    if (path === "/api/files/file-long-name/download") return route.fulfill({ status: 200, contentType: "application/octet-stream", body: "file", headers: { "Content-Disposition": "attachment; filename=long-name.docx" } });
    if (path.endsWith("/search")) return json({ query: url.searchParams.get("q"), citations: [{ citation_id: "cite-1", resource_id: ids.resource, section_title: "第八条 终止", snippet: "供应商可在任何时间书面通知后立即终止本协议。" }] });
    if (path === "/api/agent/runs") return json({ runs: [{ id: ids.run, workspace_id: "workspace-1", resource_id: ids.resource, status: "waiting_approval", objective: "检查高风险条款并生成修订建议", current_step: "CommitPatch", step_count: 4, completed_step_count: 3, failed_step_count: 0, created_at: now, updated_at: now }] });
    if (path === `/api/agent/runs/${ids.run}`) return json({ run: { id: ids.run, resource_id: ids.resource, request_id: "review-request-2026", status: "waiting_approval", objective: "检查高风险条款并生成修订建议", current_step: "CommitPatch", created_at: now, updated_at: now }, steps: [{ id: ids.step, step_key: "review:extract", step_type: "ExtractEvidence", status: "succeeded", attempt_count: 1, max_attempts: 2, created_at: now, updated_at: now }, { id: "step-2", step_key: "review:commit", step_type: "CommitPatch", status: "waiting_approval", attempt_count: 1, max_attempts: 1, created_at: now, updated_at: now }], tool_calls: [{ id: "tool-1", step_id: ids.step, tool_name: "SearchEvidence", tool_version: "v1", status: "completed", started_at: now, completed_at: now }], approvals: [{ id: ids.approval, run_id: ids.run, step_id: ids.step, tool_name: "CommitPatch", status: "pending", created_at: now }], findings: [] });
    const approval = { id: ids.approval, workspace_id: "workspace-1", run_id: ids.run, step_id: ids.step, resource_id: ids.resource, session_id: ids.session, objective: "将 2 处风险修订写入文档", tool_name: "CommitPatch", tool_version: "v1", reason: "此操作将创建新的文档版本，需要人工确认。", status: "pending", resources: [{ type: "document", id: ids.resource, access: "write" }], payload: { patch_count: 2, target_version: 3 }, created_at: now };
    if (path === "/api/agent/approvals") return json({ approvals: [approval] });
    if (path === `/api/agent/approvals/${ids.approval}` && request.method() === "GET") return json({ approval });
    if (path.endsWith("/approve") || path.endsWith("/reject")) return json({ approval: { id: ids.approval, status: path.endsWith("/approve") ? "approved" : "rejected" } });
    return json({ error: `未模拟路径 ${path}` }, 404);
  });
}

async function expectNoPageOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test.beforeEach(async ({ page }) => installApi(page));

test("renders the complete workbench without overflow", async ({ page }, testInfo) => {
  await page.goto(`/assistant/${ids.session}`);
  await expect(page.getByRole("heading", { name: "供应商框架协议审阅" })).toBeVisible();
  await expect(page.getByText("2 处高风险条款")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("assistant.png"), fullPage: true });

  await page.goto("/resources");
  await expect(page.getByRole("heading", { name: "文档库" })).toBeVisible();
  await expect(page.getByText("2026 供应商框架协议（修订稿）")).toBeVisible();
  await expectNoPageOverflow(page);

  await page.goto(`/runs/${ids.run}`);
  await expect(page.getByRole("heading", { name: "检查高风险条款并生成修订建议" })).toBeVisible();
  await expect(page.getByText("执行时间线")).toBeVisible();
  await expectNoPageOverflow(page);

  await page.goto(`/approvals/${ids.approval}`);
  await expect(page.getByRole("heading", { name: "将 2 处风险修订写入文档" })).toBeVisible();
  await expect(page.getByRole("button", { name: "批准操作" })).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("approval.png"), fullPage: true });
});

test("opens mobile navigation and preserves core actions", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only interaction");
  await page.goto(`/assistant/${ids.session}`);
  await page.getByRole("button", { name: "打开导航" }).click();
  await expect(page.getByRole("link", { name: "审批中心" })).toBeVisible();
  await page.getByRole("link", { name: "审阅台" }).click();
  await page.getByRole("button", { name: "打开文档上下文" }).click();
  await expect(page.getByRole("dialog").getByText("拖入新文档")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("assistant-mobile-context.png"), fullPage: true });
});

test("keeps long conversations scrollable and offers a jump to latest action", async ({ page }) => {
  await page.goto(`/assistant/${ids.session}`);
  const messageScroll = page.locator(".message-scroll");
  await expect(messageScroll).toBeVisible();
  expect(await messageScroll.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  const widthBefore = await messageScroll.evaluate((element) => element.clientWidth);

  await messageScroll.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll"));
  });
  const jumpLatest = page.getByRole("button", { name: "跳到最新" });
  await expect(jumpLatest).toBeVisible();
  expect(await messageScroll.evaluate((element) => element.clientWidth)).toBe(widthBefore);
  await expectNoPageOverflow(page);
  await jumpLatest.click();
  await expect.poll(() => messageScroll.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThanOrEqual(80);
  await expectNoPageOverflow(page);
});

test("detail pages return to their stable parent routes", async ({ page }) => {
  await page.goto(`/resources/${ids.resource}`);
  await page.getByRole("link", { name: "返回文档库" }).click();
  await expect(page).toHaveURL(/\/resources$/);

  await page.goto(`/runs/${ids.run}`);
  await page.getByRole("link", { name: "返回运行记录" }).click();
  await expect(page).toHaveURL(/\/runs$/);

  await page.goto(`/approvals/${ids.approval}`);
  await page.getByRole("link", { name: "返回审批中心" }).click();
  await expect(page).toHaveURL(/\/approvals$/);
});

test("deletes a document only after confirmation", async ({ page }) => {
  await page.goto(`/resources/${ids.resource}`);
  await page.getByRole("button", { name: "删除文档" }).click();
  await expect(page.getByRole("dialog", { name: "删除这份文档？" })).toBeVisible();

  const deletion = page.waitForRequest((request) => request.method() === "DELETE" && new URL(request.url()).pathname === `/api/resources/${ids.resource}`);
  await page.getByRole("button", { name: "确认删除" }).click();
  await deletion;

  await expect(page).toHaveURL(/\/resources$/);
});

test("announces async results and keeps action states explicit", async ({ page }) => {
  await page.goto(`/resources/${ids.resource}`);
  await page.getByLabel("查找证据片段").fill("终止条款");
  await page.getByRole("button", { name: "检索" }).click();
  await expect(page.getByRole("status").filter({ hasText: "找到 1 条证据片段" })).toBeVisible();

  await page.getByRole("button", { name: "导出 Markdown" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Markdown 已导出" })).toBeVisible();

  await page.goto(`/approvals/${ids.approval}`);
  await page.getByRole("button", { name: "批准操作" }).click();
  await expect(page.getByRole("button", { name: "确认批准" })).toBeDisabled();
  await page.getByLabel("决定理由").fill("风险与影响已核对");
  await page.getByRole("button", { name: "确认批准" }).click();
  await expect(page.getByRole("status").filter({ hasText: "审批已批准" })).toBeVisible();
});

test("moves focus on navigation and isolates overlay scrolling", async ({ page }) => {
  await page.goto("/resources");
  await page.getByRole("link", { name: /2026 供应商框架协议/ }).click();
  await expect(page.getByRole("heading", { name: resource.title })).toBeFocused();

  await page.goto(`/assistant/${ids.session}`);
  const iconButtons = page.locator("button:has(svg)");
  for (let index = 0; index < await iconButtons.count(); index += 1) {
    const button = iconButtons.nth(index);
    if (await button.isVisible() && !(await button.textContent())?.trim()) expect(await button.getAttribute("aria-label")).toBeTruthy();
  }
  await page.getByRole("button", { name: "删除会话" }).click();
  expect(await page.getByRole("dialog").evaluate((element) => getComputedStyle(element).overscrollBehavior)).toBe("contain");
});

test("mobile drawer and icon controls meet touch requirements", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only acceptance");
  await page.goto(`/assistant/${ids.session}`);
  await page.getByRole("button", { name: "打开导航" }).click();
  expect(await page.locator("#primary-sidebar").evaluate((element) => getComputedStyle(element).overscrollBehavior)).toBe("contain");
  const iconButtons = page.locator(".icon-button:visible");
  for (let index = 0; index < await iconButtons.count(); index += 1) {
    const size = await iconButtons.nth(index).evaluate((element) => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height }));
    expect(size.width).toBeGreaterThanOrEqual(44);
    expect(size.height).toBeGreaterThanOrEqual(44);
  }
});
