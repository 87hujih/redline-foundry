import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Search, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { PageHeader } from "../../components/PageHeader";
import { AsyncFeedback, EmptyState, ErrorState, LoadingState } from "../../components/States";
import { formatDate, shortId } from "../../lib/format";

export default function ResourceDetailPage() {
  const { resourceId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const resource = useQuery({ queryKey: ["resource", resourceId], queryFn: () => api.getResource(resourceId), enabled: Boolean(resourceId) });
  const citations = useMutation({ mutationFn: (query: string) => api.searchResource(resourceId, query) });
  const exportFile = useMutation({ mutationFn: () => api.exportResource(resourceId) });
  const remove = useMutation({
    mutationFn: () => api.deleteResource(resourceId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["resources"] });
      navigate("/resources", { replace: true });
    },
  });

  if (resource.isLoading) return <section className="page"><PageHeader eyebrow="DOCUMENT" title="读取文档" backTo="/resources" backLabel="返回文档库" /><LoadingState /></section>;
  if (resource.isError) return <section className="page"><PageHeader eyebrow="DOCUMENT" title="文档不可用" backTo="/resources" backLabel="返回文档库" /><ErrorState error={resource.error} retrying={resource.isFetching} onRetry={() => resource.refetch()} /></section>;
  if (!resource.data) return null;
  const { resource: summary, current_version: version } = resource.data;

  return (
    <section className="page">
      <PageHeader eyebrow="DOCUMENT" title={summary.title} backTo="/resources" backLabel="返回文档库" actions={<><Button variant="danger" onClick={() => setDeleteOpen(true)} disabled={remove.isPending}><Trash2 aria-hidden="true" />删除文档</Button><Button onClick={() => exportFile.mutate()} disabled={!version} loading={exportFile.isPending} loadingLabel="正在导出…"><Download aria-hidden="true" />导出 Markdown</Button></>} />
      {exportFile.isSuccess && <div className="page-feedback"><AsyncFeedback state="success" message="Markdown 已导出。" /></div>}
      {exportFile.isError && <div className="page-feedback"><AsyncFeedback state="error" message={`导出失败：${exportFile.error.message}`} /></div>}
      <div className="page-body content-width detail-grid">
        <article className="document-sheet">
          {version ? <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={{ a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}>{version.content}</ReactMarkdown></div> : <EmptyState title="没有当前版本" detail="该资源尚未生成可阅读或导出的版本。" />}
        </article>
        <aside>
          <section className="context-panel">
            <div className="context-panel-header"><span className="context-kicker">DOC FACTS</span><h2>文档信息</h2></div>
            <div className="context-panel-body"><dl className="definition-list"><dt>来源</dt><dd>{summary.source_type}</dd><dt>版本</dt><dd>{version ? `v${version.version_number}` : "—"}</dd><dt>资源 ID</dt><dd className="meta-id" title={summary.id}>{shortId(summary.id)}</dd><dt>更新时间</dt><dd>{formatDate(version?.created_at)}</dd></dl></div>
          </section>
          <section className="detail-section">
            <div className="section-title"><h2>文内检索</h2></div>
            <form className="field" onSubmit={(event) => { event.preventDefault(); if (search.trim()) citations.mutate(search.trim()); }}>
              <label htmlFor="resource-search">查找证据片段</label>
              <input id="resource-search" name="resource-search" autoComplete="off" className="input" value={search} onChange={(event) => { setSearch(event.target.value); citations.reset(); }} placeholder="例如：终止条款…" />
              <Button variant="primary" disabled={!search.trim()} loading={citations.isPending} loadingLabel="正在检索…"><Search aria-hidden="true" />检索</Button>
            </form>
            {citations.isError && <AsyncFeedback state="error" message={`检索失败：${citations.error.message}`} />}
            {citations.isSuccess && <AsyncFeedback state="success" message={citations.data.citations.length ? `找到 ${citations.data.citations.length} 条证据片段。` : "没有找到匹配片段。"} />}
            {citations.data && citations.data.citations.length > 0 && <div className="citation-list">{citations.data.citations.map((item) => <article className="citation" key={item.citation_id}><strong>{item.section_title || "未命名章节"}</strong><p>{item.snippet}</p></article>)}</div>}
          </section>
        </aside>
      </div>
      <ConfirmDialog open={deleteOpen} onOpenChange={(open) => { setDeleteOpen(open); if (open) remove.reset(); }} title="删除这份文档？" detail="文档将从文档库永久移除，使用它的会话会清除当前文档选择。此操作不能撤销。" confirmLabel="确认删除" danger busy={remove.isPending} onConfirm={() => remove.mutate()}>{remove.isError && <AsyncFeedback state="error" message={`删除失败：${remove.error.message}`} />}</ConfirmDialog>
    </section>
  );
}
