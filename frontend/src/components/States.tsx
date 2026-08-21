import { AlertCircle, AlertTriangle, CheckCircle2, FileQuestion, LoaderCircle, RotateCcw } from "lucide-react";
import { Button } from "./Button";

export function LoadingState({ label = "正在读取数据" }: { label?: string }) {
  return <div className="state-block loading-state" role="status" aria-live="polite" aria-atomic="true"><LoaderCircle className="spin" aria-hidden="true" /><span>{label}</span></div>;
}

export function EmptyState({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return <div className="state-block empty-state"><FileQuestion aria-hidden="true" /><h2>{title}</h2><p>{detail}</p>{action}</div>;
}

export function ErrorState({ error, onRetry, retrying = false }: { error: unknown; onRetry?: () => void; retrying?: boolean }) {
  const message = error instanceof Error ? error.message : "发生未知错误";
  return (
    <div className="state-block error-state" role="alert" aria-live="assertive" aria-atomic="true">
      <AlertTriangle aria-hidden="true" /><h2>无法读取当前内容</h2><p>{message}</p>
      {onRetry && <Button onClick={onRetry} loading={retrying} loadingLabel="正在重试…"><RotateCcw aria-hidden="true" />重试</Button>}
    </div>
  );
}

export function AsyncFeedback({ state, message, id }: { state: "loading" | "success" | "error"; message: string; id?: string }) {
  const Icon = state === "loading" ? LoaderCircle : state === "success" ? CheckCircle2 : AlertCircle;
  return <p id={id} className={`async-feedback async-feedback-${state}`} role={state === "error" ? "alert" : "status"} aria-live={state === "error" ? "assertive" : "polite"} aria-atomic="true"><Icon className={state === "loading" ? "spin" : undefined} aria-hidden="true" /><span>{message}</span></p>;
}
