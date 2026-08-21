import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./AppShell";
import { LoadingState } from "../components/States";

const AssistantPage = lazy(() => import("../features/assistant/AssistantPage"));
const ResourcesPage = lazy(() => import("../features/resources/ResourcesPage"));
const ResourceDetailPage = lazy(() => import("../features/resources/ResourceDetailPage"));
const RunsPage = lazy(() => import("../features/runs/RunsPage"));
const RunDetailPage = lazy(() => import("../features/runs/RunDetailPage"));
const ApprovalsPage = lazy(() => import("../features/approvals/ApprovalsPage"));
const ApprovalDetailPage = lazy(() => import("../features/approvals/ApprovalDetailPage"));

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/assistant" replace />} />
        <Route path="assistant" element={<Suspense fallback={<LoadingState label="正在打开审阅台" />}><AssistantPage /></Suspense>} />
        <Route path="assistant/:sessionId" element={<Suspense fallback={<LoadingState label="正在恢复会话" />}><AssistantPage /></Suspense>} />
        <Route path="resources" element={<Suspense fallback={<LoadingState />}><ResourcesPage /></Suspense>} />
        <Route path="resources/:resourceId" element={<Suspense fallback={<LoadingState />}><ResourceDetailPage /></Suspense>} />
        <Route path="runs" element={<Suspense fallback={<LoadingState />}><RunsPage /></Suspense>} />
        <Route path="runs/:runId" element={<Suspense fallback={<LoadingState />}><RunDetailPage /></Suspense>} />
        <Route path="approvals" element={<Suspense fallback={<LoadingState />}><ApprovalsPage /></Suspense>} />
        <Route path="approvals/:approvalId" element={<Suspense fallback={<LoadingState />}><ApprovalDetailPage /></Suspense>} />
        <Route path="*" element={<Navigate to="/assistant" replace />} />
      </Route>
    </Routes>
  );
}
