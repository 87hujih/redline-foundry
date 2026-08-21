import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, FileText, Search } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { PageHeader } from "../../components/PageHeader";
import { formatDate } from "../../lib/format";

export default function ResourcesPage() {
  const [query, setQuery] = useState("");
  const resources = useQuery({ queryKey: ["resources"], queryFn: api.listResources });
  const filtered = useMemo(() => resources.data?.filter((item) => item.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) || [], [resources.data, query]);

  return (
    <section className="page">
      <PageHeader eyebrow="LIBRARY / 01" title="文档库" />
      <div className="page-body content-width">
        <div className="filter-bar">
          <label className="search-field"><span className="sr-only">筛选文档</span><Search aria-hidden="true" /><input name="resource-filter" autoComplete="off" className="input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按标题筛选文档…" /></label>
          <span className="data-meta">{filtered.length} 份文档</span>
        </div>
        {resources.isLoading && <LoadingState label="正在整理文档库" />}
        {resources.isError && <ErrorState error={resources.error} retrying={resources.isFetching} onRetry={() => resources.refetch()} />}
        {!resources.isLoading && !resources.isError && filtered.length === 0 && <EmptyState title={query ? "没有匹配文档" : "文档库还是空的"} detail={query ? "尝试缩短关键词，或清除当前筛选。" : "从审阅台上传第一份文档后，它会出现在这里。"} />}
        {filtered.length > 0 && <div className="resource-grid">
          {filtered.map((resource) => (
            <Link key={resource.id} to={`/resources/${resource.id}`} className="resource-card">
              <div className="resource-card-top"><span className="resource-card-icon"><FileText aria-hidden="true" /></span><span className="status-badge">{resource.source_type === "upload" ? "上传文档" : resource.source_type}</span></div>
              <h2 title={resource.title}>{resource.title}</h2>
              <p className="meta-id">{resource.id}</p>
              <div className="resource-card-footer"><time dateTime={resource.created_at}>{formatDate(resource.created_at)}</time><ArrowUpRight aria-hidden="true" /></div>
            </Link>
          ))}
        </div>}
      </div>
    </section>
  );
}
