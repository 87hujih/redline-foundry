import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, Check, ShieldAlert, X } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { AsyncFeedback, ErrorState, LoadingState } from "../../components/States";
import { formatDate, shortId } from "../../lib/format";

export default function ApprovalDetailPage() {
  const { approvalId = "" } = useParams();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const approval = useQuery({ queryKey: ["approval", approvalId], queryFn: () => api.getApproval(approvalId), enabled: Boolean(approvalId) });
  const decide = useMutation({
    mutationFn: ({ decision, value }: { decision: "approve" | "reject"; value: string }) => api.decideApproval(approvalId, decision, value),
    onSuccess: async () => { setDialog(null); setReason(""); await Promise.all([queryClient.invalidateQueries({ queryKey: ["approval", approvalId] }), queryClient.invalidateQueries({ queryKey: ["approvals"] })]); },
  });
  if (approval.isLoading) return <section className="page"><PageHeader eyebrow="APPROVAL" title="读取审批详情" backTo="/approvals" backLabel="返回审批中心" /><LoadingState /></section>;
  if (approval.isError) return <section className="page"><PageHeader eyebrow="APPROVAL" title="审批不可用" backTo="/approvals" backLabel="返回审批中心" /><ErrorState error={approval.error} retrying={approval.isFetching} onRetry={() => approval.refetch()} /></section>;
  if (!approval.data) return null;
  const item = approval.data;
  const submit = () => { if (dialog && reason.trim()) decide.mutate({ decision: dialog, value: reason.trim() }); };
  return (
    <section className="page">
      <PageHeader eyebrow={`APPROVAL / ${shortId(item.id)}`} title={item.objective} backTo="/approvals" backLabel="返回审批中心" actions={<StatusBadge status={item.status} />} />
      {decide.isSuccess && <div className="page-feedback"><AsyncFeedback state="success" message={decide.variables?.decision === "approve" ? "审批已批准。" : "审批已拒绝。"} /></div>}
      <div className="page-body content-width detail-grid">
        <div>
          <section className="approval-callout"><ShieldAlert aria-hidden="true" /><h2>Agent 请求执行受控操作</h2><p>{item.reason}</p>{item.status === "pending" && <div className="decision-bar"><Button variant="primary" onClick={() => { decide.reset(); setDialog("approve"); }}><Check aria-hidden="true" />批准操作</Button><Button variant="danger" onClick={() => { decide.reset(); setDialog("reject"); }}><X aria-hidden="true" />拒绝操作</Button></div>}</section>
          <section className="detail-section"><div className="section-title"><h2>操作范围</h2></div><dl className="definition-list"><dt>工具</dt><dd>{item.tool_name} / {item.tool_version}</dd><dt>Run</dt><dd><Link to={`/runs/${item.run_id}`}>{shortId(item.run_id)} <ArrowUpRight aria-hidden="true" style={{ display: "inline", width: 14 }} /></Link></dd><dt>Step</dt><dd className="meta-id">{shortId(item.step_id)}</dd><dt>Resource</dt><dd className="meta-id">{shortId(item.resource_id)}</dd></dl></section>
          <section className="detail-section"><div className="section-title"><h2>资源声明</h2></div><pre className="json-preview">{JSON.stringify(item.resources, null, 2)}</pre></section>
          <section className="detail-section"><div className="section-title"><h2>公开请求数据</h2></div><pre className="json-preview">{JSON.stringify(item.payload, null, 2)}</pre></section>
        </div>
        <aside className="context-panel"><div className="context-panel-header"><span className="context-kicker">DECISION FACTS</span><h2>审批事实</h2></div><div className="context-panel-body"><dl className="definition-list"><dt>状态</dt><dd><StatusBadge status={item.status} /></dd><dt>创建时间</dt><dd>{formatDate(item.created_at)}</dd><dt>决定时间</dt><dd>{formatDate(item.decided_at)}</dd><dt>决定理由</dt><dd>{item.decision_reason || "—"}</dd><dt>审批 ID</dt><dd className="meta-id">{shortId(item.id)}</dd></dl></div></aside>
      </div>
      <ConfirmDialog open={dialog !== null} onOpenChange={(open) => !open && setDialog(null)} title={dialog === "approve" ? "批准这项操作？" : "拒绝这项操作？"} detail="决定提交后不能在当前界面撤销。请记录可审计的理由。" confirmLabel={dialog === "approve" ? "确认批准" : "确认拒绝"} danger={dialog === "reject"} busy={decide.isPending} confirmDisabled={!reason.trim()} onConfirm={submit}>
        <div className="field"><label htmlFor="decision-reason">决定理由</label><textarea id="decision-reason" name="decision-reason" autoComplete="off" className="textarea" value={reason} onChange={(event) => setReason(event.target.value)} aria-describedby={decide.isError ? "decision-error" : "decision-hint"} />{!reason.trim() && <span id="decision-hint" className="data-meta">理由不能为空</span>}{decide.isError && <AsyncFeedback id="decision-error" state="error" message={`提交失败：${decide.error.message}`} />}</div>
      </ConfirmDialog>
    </section>
  );
}
