import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { PageHeader } from "../../components/PageHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { formatDate, shortId } from "../../lib/format";

const statuses = [
  ["", "全部状态"], ["queued", "排队中"], ["running", "运行中"], ["waiting_input", "等待输入"],
  ["waiting_approval", "等待审批"], ["succeeded", "已完成"], ["failed", "失败"], ["cancelled", "已取消"],
];

export default function RunsPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const runs = useQuery({ queryKey: ["runs", status], queryFn: () => api.listRuns({ status, limit: 50 }) });
  return (
    <section className="page">
      <PageHeader eyebrow="OPERATIONS / 02" title="运行记录" />
      <div className="page-body content-width">
        <div className="filter-bar"><div className="field"><label htmlFor="run-status">运行状态</label><select id="run-status" name="run-status" className="select" value={status} onChange={(event) => setParams(event.target.value ? { status: event.target.value } : {})}>{statuses.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><span className="data-meta">最近 50 条运行</span></div>
        {runs.isLoading && <LoadingState label="正在读取运行记录" />}
        {runs.isError && <ErrorState error={runs.error} retrying={runs.isFetching} onRetry={() => runs.refetch()} />}
        {runs.data?.length === 0 && <EmptyState title="没有运行记录" detail={status ? "当前筛选下没有记录。" : "发送一次文档审阅请求后，运行会出现在这里。"} />}
        {runs.data && runs.data.length > 0 && <div className="data-list" role="list">
          {runs.data.map((run) => {
            const progress = run.step_count ? Math.round((run.completed_step_count / run.step_count) * 100) : 0;
            return <Link className="data-row" role="listitem" key={run.id} to={`/runs/${run.id}`}>
              <div className="data-primary"><strong title={run.objective}>{run.objective}</strong><span className="meta-id">{shortId(run.id)} · {run.current_step || "等待调度"}</span></div>
              <StatusBadge status={run.status} />
              <div className="run-progress"><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><small>{run.completed_step_count}/{run.step_count} 步 · {formatDate(run.updated_at)}</small></div>
              <ChevronRight aria-hidden="true" />
            </Link>;
          })}
        </div>}
      </div>
    </section>
  );
}
