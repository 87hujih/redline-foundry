import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, Bot, FileText, MessageSquareText, Paperclip, PanelRight, Send, Trash2, Upload, User, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { useNavigate, useParams } from "react-router-dom";
import type { Conversation, JsonValue, Message, Resource, SSEFrame } from "../../api/types";
import { api, ApiError } from "../../api/client";
import { newRequestId, streamTurn } from "../../api/sse";
import { Button, IconButton } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { AsyncFeedback, ErrorState, LoadingState } from "../../components/States";
import { formatDate } from "../../lib/format";

const starters = ["提炼这份文档的关键结论", "找出风险条款并说明理由", "检查前后不一致的表述"];

export default function AssistantPage() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [selectedResource, setSelectedResource] = useState("");
  const [pendingUser, setPendingUser] = useState("");
  const [streamText, setStreamText] = useState("");
  const [turnStatus, setTurnStatus] = useState("");
  const [turnError, setTurnError] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const [showLatest, setShowLatest] = useState(false);

  const conversation = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.getSession(sessionId!), enabled: Boolean(sessionId) });
  const resources = useQuery({ queryKey: ["resources"], queryFn: api.listResources });
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.getCapabilities });
  const selection = useQuery({ queryKey: ["selection", sessionId], queryFn: () => api.getSelection(sessionId!), enabled: Boolean(sessionId) });

  useEffect(() => {
    if (selection.data) setSelectedResource(selection.data.resource_id || "");
  }, [selection.data]);
  useEffect(() => () => abortRef.current?.abort(), [sessionId]);

  function handleMessageScroll() {
    const element = messageScrollRef.current;
    if (!element) return;
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    const atBottom = distanceFromBottom <= 80;
    followLatestRef.current = atBottom;
    setShowLatest(!atBottom && distanceFromBottom > 0);
  }

  function scrollToLatest() {
    const element = messageScrollRef.current;
    if (!element) return;
    followLatestRef.current = true;
    element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
    setShowLatest(false);
  }

  const selectResource = useMutation({
    mutationFn: (resourceId: string) => sessionId ? api.setSelection(sessionId, resourceId) : Promise.resolve({ resource_id: resourceId }),
    onSuccess: (result) => { setSelectedResource(result.resource_id); if (sessionId) queryClient.setQueryData(["selection", sessionId], result); },
  });

  const upload = useMutation({
    mutationFn: (file: File) => {
      if (file.size > 20 * 1024 * 1024) throw new Error("文件不能超过 20 MiB");
      const allowed = capabilities.data?.supported_extensions || [];
      const extension = file.name.includes(".") ? `.${file.name.split(".").pop()?.toLowerCase()}` : "";
      if (allowed.length && !allowed.map((item) => item.toLowerCase()).includes(extension)) throw new Error(`不支持 ${extension || "无扩展名"} 文件`);
      return api.uploadFile(file, sessionId);
    },
    onSuccess: async (result) => {
      queryClient.setQueryData(["session", result.session.id], { session: result.session, messages: result.messages });
      if (result.resource) {
        setSelectedResource(result.resource.id);
        queryClient.setQueryData(["selection", result.session.id], { resource_id: result.resource.id });
      }
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["sessions"] }), queryClient.invalidateQueries({ queryKey: ["resources"] })]);
      if (!sessionId) navigate(`/assistant/${result.session.id}`, { replace: true });
    },
  });

  const remove = useMutation({ mutationFn: () => api.deleteSession(sessionId!), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["sessions"] }); navigate("/assistant", { replace: true }); } });

  async function send() {
    const message = draft.trim();
    if (!message || !selectedResource || turnStatus) return;
    setDraft(""); setPendingUser(message); setStreamText(""); setTurnError(""); setTurnStatus("正在接受审阅请求");
    try {
      if (!sessionId) {
        const requestId = newRequestId();
        let result: Conversation | undefined;
        for (let attempt = 0; attempt < 12 && !result; attempt += 1) {
          try {
            result = await api.createConversation(message, selectedResource, requestId);
          } catch (error) {
            const retryable = error instanceof ApiError
              && error.status === 503
              && (error.message.includes("相同请求 ID") || error.message.toLowerCase().includes("same request id"));
            if (!retryable || attempt === 11) throw error;
            setTurnStatus("审阅仍在进行，正在恢复结果");
            await wait(500, new AbortController().signal);
          }
        }
        if (!result) throw new Error("审阅请求未返回结果");
        queryClient.setQueryData(["session", result.session.id], result);
        await queryClient.invalidateQueries({ queryKey: ["sessions"] });
        navigate(`/assistant/${result.session.id}`, { replace: true });
        return;
      }
      const requestId = newRequestId();
      let cursor = 0;
      let terminal = false;
      let terminalError = false;
      const controller = new AbortController();
      abortRef.current = controller;
      const onFrame = (frame: SSEFrame) => {
        if (frame.event === "turn_state") setTurnStatus(turnStatusLabel(frame.data.status));
        if (frame.event === "message_delta") setStreamText((current) => current + stringValue(frame.data.delta || frame.data.content));
        if (frame.event === "message_completed" && isMessage(frame.data.message)) {
          const complete = frame.data.message;
          queryClient.setQueryData<Conversation>(["session", sessionId], (current) => current ? { ...current, messages: mergeMessages(current.messages, complete) } : current);
          setStreamText("");
        }
        if (frame.event === "error") { terminal = true; terminalError = true; setTurnError(stringValue(frame.data.message) || "审阅中断，请重试"); }
        if (frame.event === "done") terminal = true;
      };
      for (let attempt = 0; attempt < 3 && !terminal; attempt += 1) {
        try {
          cursor = await streamTurn({ sessionId, message, resourceId: selectedResource, requestId, afterId: cursor, signal: controller.signal, onFrame });
          if (!terminal && attempt < 2) await wait(300 * (attempt + 1), controller.signal);
        } catch (error) {
          if (controller.signal.aborted) throw error;
          if (attempt === 2) throw error;
          await wait(300 * (attempt + 1), controller.signal);
        }
      }
      if (!terminal && !terminalError) throw new Error("连接中断，未收到完成事件");
      await queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setTurnError(error instanceof Error ? error.message : "审阅请求失败");
    } finally {
      abortRef.current = null; setTurnStatus(""); setPendingUser("");
    }
  }

  const title = conversation.data?.session.title || "新建审阅";
  const messages = conversation.data?.messages || [];
  const latestMessageId = messages.at(-1)?.id;
  const availableResources = (resources.data || []).filter((item) => item.source_type === "upload");
  const contextFeedback = upload.isPending ? { state: "loading" as const, message: "正在上传并处理文档…" }
    : selectResource.isPending ? { state: "loading" as const, message: "正在切换当前文档…" }
      : upload.isError ? { state: "error" as const, message: `上传失败：${upload.error.message}` }
        : selectResource.isError ? { state: "error" as const, message: `切换失败：${selectResource.error.message}` }
          : upload.isSuccess ? { state: "success" as const, message: "文档已上传并设为当前文档。" }
            : selectResource.isSuccess ? { state: "success" as const, message: "当前文档已切换。" } : null;
  const contextProps = { resources: availableResources, selected: selectedResource, capabilitiesHint: capabilities.data?.hint || "正在读取支持格式", accept: capabilities.data?.accept || "", pending: selectResource.isPending || upload.isPending, feedback: contextFeedback, dragging, onDragging: setDragging, onSelect: (value: string) => { upload.reset(); selectResource.reset(); selectResource.mutate(value); }, onFile: (file: File) => { selectResource.reset(); upload.reset(); upload.mutate(file); }, onChooseFile: () => fileInput.current?.click() };

  useEffect(() => {
    followLatestRef.current = true;
    setShowLatest(false);
  }, [sessionId]);

  useEffect(() => {
    const element = messageScrollRef.current;
    if (!element || !followLatestRef.current) return;
    element.scrollTop = element.scrollHeight;
    setShowLatest(false);
  }, [latestMessageId, messages.length, pendingUser, streamText]);

  if (sessionId && conversation.isLoading) return <section className="page"><PageHeader eyebrow="ASSISTANT" title="恢复审阅" backTo="/assistant" backLabel="返回审阅台" /><LoadingState label="正在恢复会话与消息" /></section>;
  if (sessionId && conversation.isError) return <section className="page"><PageHeader eyebrow="ASSISTANT" title="会话不可用" backTo="/assistant" backLabel="返回审阅台" /><ErrorState error={conversation.error} retrying={conversation.isFetching} onRetry={() => conversation.refetch()} /></section>;

  return (
    <section className="page assistant-page">
      <PageHeader eyebrow="WORKBENCH / 00" title={title} backTo={sessionId ? "/assistant" : undefined} backLabel="返回审阅台" actions={<><IconButton className="context-toggle" label="打开文档上下文" onClick={() => setContextOpen(true)}><PanelRight /></IconButton>{sessionId && <IconButton label="删除会话" onClick={() => setDeleteOpen(true)}><Trash2 /></IconButton>}</>} />
      <div className="assistant-layout">
        <div className="conversation">
          <div ref={messageScrollRef} className="message-scroll" onScroll={handleMessageScroll}>
            {messages.length === 0 && !pendingUser ? <div className="conversation-empty"><span className="empty-index">REVIEW / READY</span><h2>把文档放在案头，开始一次有证据的审阅。</h2><p>{selectedResource ? "当前文档已就绪。你可以直接提出审阅目标，也可以从下面的常用任务开始。" : "先在右侧选择或上传一份文档，再向审阅 Agent 提出具体目标。"}</p><div className="starter-actions">{starters.map((text) => <button key={text} onClick={() => setDraft(text)}>{text}</button>)}</div></div>
              : <div className="message-list">{messages.map((message) => <MessageItem key={message.id} message={message} onDownload={(id) => api.downloadFile(id)} />)}{pendingUser && <MessageItem message={{ id: "pending-user", role: "user", kind: "text", payload: { content: pendingUser }, sequence_no: Number.MAX_SAFE_INTEGER - 1, created_at: new Date().toISOString() }} />}{streamText && <MessageItem message={{ id: "stream-assistant", role: "assistant", kind: "text", payload: { content: streamText }, sequence_no: Number.MAX_SAFE_INTEGER, created_at: new Date().toISOString() }} />}</div>}
          </div>
          {showLatest && <button className="jump-latest" type="button" onClick={scrollToLatest}><ArrowDown aria-hidden="true" />跳到最新</button>}
          <div className="composer-wrap">
            <div className="turn-status" role="status" aria-live="polite" aria-atomic="true">{turnStatus && <><span className="status-dot" />{turnStatus}</>}</div>
            {turnError && <AsyncFeedback state="error" message={turnError} />}
            <div className="composer"><IconButton label={upload.isPending ? "正在上传文档" : "上传文档"} loading={upload.isPending} onClick={() => fileInput.current?.click()}><Paperclip /></IconButton><textarea name="review-message" autoComplete="off" maxLength={4000} aria-label="审阅消息" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={selectedResource ? "输入具体的审阅目标…" : "请先选择或上传文档"} disabled={!selectedResource || Boolean(turnStatus)} /><IconButton className="send-button" label={turnStatus ? "正在处理消息" : "发送消息"} loading={Boolean(turnStatus)} disabled={!draft.trim() || !selectedResource} onClick={() => void send()}><Send /></IconButton></div>
            <div className="composer-hint"><span>{selectedResource ? "Enter 发送 · Shift+Enter 换行" : "需要一份当前文档"}</span><span>{draft.length}/4000</span></div>
          </div>
        </div>
        <aside className="assistant-context"><ResourceContext {...contextProps} selectId="resource-selection-desktop" /></aside>
      </div>
      <input ref={fileInput} className="sr-only" type="file" name="review-document" aria-label="选择审阅文档" accept={capabilities.data?.accept} onChange={(event) => { const file = event.target.files?.[0]; if (file) { selectResource.reset(); upload.reset(); upload.mutate(file); } event.target.value = ""; }} />
      <Dialog.Root open={contextOpen} onOpenChange={setContextOpen}><Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="dialog-content context-dialog"><div className="dialog-header"><Dialog.Title>当前文档</Dialog.Title><Dialog.Close asChild><IconButton label="关闭"><X /></IconButton></Dialog.Close></div><ResourceContext {...contextProps} selectId="resource-selection-dialog" /></Dialog.Content></Dialog.Portal></Dialog.Root>
      <ConfirmDialog open={deleteOpen} onOpenChange={(open) => { setDeleteOpen(open); if (open) remove.reset(); }} title="删除这条会话？" detail="会话将从审阅台移除。此操作不能在当前界面撤销。" confirmLabel="确认删除" danger busy={remove.isPending} onConfirm={() => remove.mutate()}>{remove.isError && <AsyncFeedback state="error" message={`删除失败：${remove.error.message}`} />}</ConfirmDialog>
    </section>
  );
}

function ResourceContext({ resources, selected, capabilitiesHint, accept, pending, feedback, dragging, onDragging, onSelect, onFile, onChooseFile, selectId }: { resources: Resource[]; selected: string; capabilitiesHint: string; accept: string; pending: boolean; feedback: { state: "loading" | "success" | "error"; message: string } | null; dragging: boolean; onDragging: (value: boolean) => void; onSelect: (id: string) => void; onFile: (file: File) => void; onChooseFile: () => void; selectId: string }) {
  const current = resources.find((item) => item.id === selected);
  return <div className="resource-select-card"><span className="context-kicker">CURRENT SOURCE</span><h2>当前文档</h2><p>每次审阅只绑定一份文档，切换不会影响历史 Run。</p><div className="field"><label htmlFor={selectId}>选择已有文档</label><select id={selectId} name={selectId} className="select" value={selected} disabled={pending} aria-busy={pending || undefined} onChange={(event) => onSelect(event.target.value)}><option value="">请选择文档</option>{resources.map((resource) => <option key={resource.id} value={resource.id}>{resource.title}</option>)}</select></div>{current && <div className="resource-facts"><div className="resource-fact"><span>标题</span><strong title={current.title}>{current.title}</strong></div><div className="resource-fact"><span>来源</span><span>{current.source_type}</span></div></div>}<div className={`drop-zone ${dragging ? "dragging" : ""}`} onDragEnter={(event) => { event.preventDefault(); onDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => onDragging(false)} onDrop={(event) => { event.preventDefault(); onDragging(false); const file = event.dataTransfer.files[0]; if (file) onFile(file); }}><Upload aria-hidden="true" /><strong>{pending ? "正在处理文档…" : "拖入新文档"}</strong><small>{capabilitiesHint}</small><Button onClick={onChooseFile} loading={pending} loadingLabel="正在处理…">选择文件</Button><span className="sr-only">{accept}</span></div>{feedback && <AsyncFeedback state={feedback.state} message={feedback.message} />}</div>;
}

function MessageItem({ message, onDownload }: { message: Message; onDownload?: (id: string) => Promise<void> }) {
  const [downloadState, setDownloadState] = useState<"idle" | "loading" | "success" | "error">("idle");
  const user = message.role === "user";
  const content = stringValue(message.payload.content);
  const filename = stringValue(message.payload.filename || message.payload.original_filename);
  const fileId = stringValue(message.payload.file_id || message.payload.id);
  const download = async () => {
    if (!fileId || !onDownload) return;
    setDownloadState("loading");
    try { await onDownload(fileId); setDownloadState("success"); }
    catch { setDownloadState("error"); }
  };
  return <article className={`message message-${user ? "user" : "assistant"}`}><div className="message-avatar" aria-hidden="true">{user ? <User /> : <Bot />}</div><div><div className="message-heading"><strong>{user ? "你" : "审阅 Agent"}</strong><time dateTime={message.created_at}>{formatDate(message.created_at)}</time></div><div className={`message-body ${message.kind === "system" ? "system-message" : ""}`}>{message.kind === "text" ? <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={{ a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}>{content}</ReactMarkdown> : message.kind === "session_file" ? <div className="file-message"><FileText aria-hidden="true" /><div className="file-message-content"><strong title={filename || "已上传文档"}>{filename || "已上传文档"}</strong><p className="data-meta">文档已加入当前会话</p>{downloadState === "success" && <AsyncFeedback state="success" message="文件下载已开始。" />}{downloadState === "error" && <AsyncFeedback state="error" message="文件下载失败，请重试。" />}</div>{fileId && onDownload && <Button variant="ghost" loading={downloadState === "loading"} loadingLabel="下载中…" onClick={() => void download()}>下载</Button>}</div> : message.kind === "system" ? <p>{content}</p> : <div className="file-message"><MessageSquareText aria-hidden="true" /><div className="file-message-content"><strong>{messageKindLabel(message.kind)}</strong><p>{content || "此消息类型暂不支持完整展示。"}</p></div></div>}</div></div></article>;
}

function mergeMessages(messages: Message[], incoming: Message): Message[] {
  return [...messages.filter((item) => item.id !== incoming.id), incoming].sort((a, b) => a.sequence_no - b.sequence_no);
}
function isMessage(value: unknown): value is Message {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.id === "string" && typeof candidate.sequence_no === "number";
}
function stringValue(value: JsonValue | undefined): string { return typeof value === "string" ? value : ""; }
function turnStatusLabel(value: JsonValue | undefined): string { const status = stringValue(value); return status === "running" ? "正在审阅文档" : status === "waiting_approval" ? "等待人工审批" : status === "waiting_input" ? "等待补充信息" : status ? `运行状态：${status}` : "正在处理"; }
function messageKindLabel(kind: string): string { return ({ task_suggestion: "任务建议", task_created: "任务已创建", task_status: "任务状态" } as Record<string, string>)[kind] || "兼容消息"; }
function wait(ms: number, signal: AbortSignal): Promise<void> { return new Promise((resolve, reject) => { const timer = window.setTimeout(resolve, ms); signal.addEventListener("abort", () => { window.clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); }, { once: true }); }); }
