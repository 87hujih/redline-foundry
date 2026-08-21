import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, Wrench } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { PageHeader } from "../../components/PageHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { ErrorState, LoadingState } from "../../components/States";
import { formatDate, shortId } from "../../lib/format";

export default function RunDetailPage() {
  const { runId = "" } = useParams();
  const detail = useQuery({ queryKey: ["run", runId], queryFn: () => api.getRun(runId), enabled: Boolean(runId) });
  if (detail.isLoading) return <section className="page"><PageHeader eyebrow="RUN" title="读取运行详情" backTo="/runs" backLabel="返回运行记录" /><LoadingState /></section>;
  if (detail.isError) return <section className="page"><PageHeader eyebrow="RUN" title="运行不可用" backTo="/runs" backLabel="返回运行记录" /><ErrorState error={detail.error} retrying={detail.isFetching} onRetry={() => detail.refetch()} /></section>;
  if (!detail.data) return null;
  const { run, steps, tool_calls: tools, approvals, findings } = detail.data;
  return (
    <section className="page">
      <PageHeader eyebrow={`RUN / ${shortId(run.id)}`} title={run.objective} backTo="/runs" backLabel="返回运行记录" actions={<StatusBadge status={run.status} />} />
      <div className="page-body content-width detail-grid">
        <div>
          <section className="detail-section"><div className="section-title"><h2>执行时间线</h2><span className="data-meta">{steps.length} 个步骤</span></div><div className="timeline">
            {steps.map((step) => <article className="timeline-item" key={step.id}><span className={`timeline-marker ${step.status === "succeeded" ? "done" : step.status === "failed" ? "failed" : ""}`} /><div className="timeline-content"><h3>{step.step_type}</h3><p>{step.step_key} · 尝试 {step.attempt_count}/{step.max_attempts}</p>{step.next_retry_at && <p>下次重试：{formatDate(step.next_retry_at)}</p>}</div><StatusBadge status={step.status} /></article>)}
          </div></section>
          <section className="detail-section"><div className="section-title"><h2>工具调用</h2><span className="data-meta">{tools.length} 次</span></div>{tools.length === 0 ? <p className="data-meta">没有公开的工具调用。</p> : <div className="data-list">{tools.map((tool) => <div className="data-row" key={tool.id}><div className="data-primary"><strong><Wrench aria-hidden="true" style={{ display: "inline", width: 14, marginRight: 8 }} />{tool.tool_name}</strong><span>{tool.tool_version} · {shortId(tool.step_id)}</span></div><StatusBadge status={tool.status} /><span className="data-meta">{tool.error_category || formatDate(tool.completed_at || tool.started_at)}</span><span /></div>)}</div>}</section>
          {findings.length > 0 && <section className="detail-section"><div className="section-title"><h2>运行发现</h2></div>{findings.map((finding) => <div className={`finding ${finding.severity}`} key={`${finding.code}-${finding.message}`}><AlertTriangle aria-hidden="true" /><div><strong>{finding.code}</strong><p>{finding.message}</p></div></div>)}</section>}
        </div>
        <aside className="context-panel"><div className="context-panel-header"><span className="context-kicker">RUN FACTS</span><h2>运行事实</h2></div><div className="context-panel-body"><dl className="definition-list"><dt>运行 ID</dt><dd className="meta-id">{shortId(run.id)}</dd><dt>创建时间</dt><dd>{formatDate(run.created_at)}</dd><dt>更新时间</dt><dd>{formatDate(run.updated_at)}</dd><dt>请求 ID</dt><dd className="meta-id">{shortId(run.request_id)}</dd><dt>当前步骤</dt><dd>{run.current_step || "—"}</dd></dl>
          {run.resource_id && <Link className="button button-secondary" style={{ marginTop: 16 }} to={`/resources/${run.resource_id}`}>查看关联文档<ArrowUpRight aria-hidden="true" /></Link>}
          {approvals.length > 0 && <div className="detail-section"><h3>关联审批</h3>{approvals.map((approval) => <Link key={approval.id} className="session-link" to={`/approvals/${approval.id}`}><span>{approval.tool_name}</span><StatusBadge status={approval.status} /></Link>)}</div>}
        </div></aside>
      </div>
    </section>
  );
}
