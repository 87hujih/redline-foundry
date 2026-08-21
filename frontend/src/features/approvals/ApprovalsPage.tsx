import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ShieldAlert } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { PageHeader } from "../../components/PageHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { formatDate, shortId } from "../../lib/format";

const statuses = [["", "全部状态"], ["pending", "待审批"], ["approved", "已批准"], ["rejected", "已拒绝"], ["cancelled", "已取消"]];

export default function ApprovalsPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const approvals = useQuery({ queryKey: ["approvals", status], queryFn: () => api.listApprovals({ status, limit: 50 }) });
  const pending = approvals.data?.filter((item) => item.status === "pending").length || 0;
  return (
    <section className="page">
      <PageHeader eyebrow="CONTROL / 03" title="审批中心" actions={pending > 0 ? <span className="status-badge status-pending"><ShieldAlert aria-hidden="true" />{pending} 项待处理</span> : undefined} />
      <div className="page-body content-width">
        <div className="filter-bar"><div className="field"><label htmlFor="approval-status">审批状态</label><select id="approval-status" name="approval-status" className="select" value={status} onChange={(event) => setParams(event.target.value ? { status: event.target.value } : {})}>{statuses.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><span className="data-meta">最近 50 条审批</span></div>
        {approvals.isLoading && <LoadingState label="正在读取审批队列" />}
        {approvals.isError && <ErrorState error={approvals.error} retrying={approvals.isFetching} onRetry={() => approvals.refetch()} />}
        {approvals.data?.length === 0 && <EmptyState title="审批队列为空" detail={status ? "当前筛选下没有审批记录。" : "需要人工确认的 Agent 操作会出现在这里。"} />}
        {approvals.data && approvals.data.length > 0 && <div className="data-list" role="list">{approvals.data.map((approval) => <Link className="data-row" role="listitem" key={approval.id} to={`/approvals/${approval.id}`}><div className="data-primary"><strong title={approval.objective}>{approval.objective}</strong><span>{approval.tool_name} · {shortId(approval.run_id)}</span></div><StatusBadge status={approval.status} /><span className="data-meta">{formatDate(approval.created_at)}</span><ChevronRight aria-hidden="true" /></Link>)}</div>}
      </div>
    </section>
  );
}
