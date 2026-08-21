import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Archive, BotMessageSquare, ClipboardCheck, Menu, PanelLeftClose, Plus, X } from "lucide-react";
import { api } from "../api/client";
import { IconButton } from "../components/Button";
import { formatRelativeTime } from "../lib/format";

const nav = [
  { to: "/assistant", label: "审阅台", icon: BotMessageSquare },
  { to: "/resources", label: "文档库", icon: Archive },
  { to: "/runs", label: "运行记录", icon: PanelLeftClose },
  { to: "/approvals", label: "审批中心", icon: ClipboardCheck },
];

export function AppShell() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.listSessions });

  useEffect(() => {
    setOpen(false);
    const frame = window.requestAnimationFrame(() => {
      const target = mainRef.current?.querySelector<HTMLElement>("h1") || mainRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.pathname]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="mobile-header">
        <IconButton label="打开导航" aria-expanded={open} aria-controls="primary-sidebar" onClick={() => setOpen(true)}><Menu /></IconButton>
        <Brand />
        <NavLink className="icon-button" aria-label="新建审阅" title="新建审阅" to="/assistant"><Plus aria-hidden="true" /></NavLink>
      </header>
      {open && <button className="drawer-scrim" aria-label="关闭导航" onClick={() => setOpen(false)} />}
      <aside id="primary-sidebar" className={`sidebar ${open ? "is-open" : ""}`} aria-label="主导航">
        <div className="sidebar-top">
          <Brand />
          <IconButton className="mobile-only" label="关闭导航" onClick={() => setOpen(false)}><X /></IconButton>
        </div>
        <nav className="primary-nav">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} onClick={() => setOpen(false)} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}>
              <Icon aria-hidden="true" /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-rule" />
        <div className="session-heading">
          <span>近期会话</span>
          <NavLink className="icon-button compact" to="/assistant" title="新建审阅" aria-label="新建审阅"><Plus aria-hidden="true" /></NavLink>
        </div>
        <div className="session-list">
          {sessions.isLoading && <span className="sidebar-note" role="status" aria-live="polite">正在读取会话…</span>}
          {sessions.isError && <button className="text-action" disabled={sessions.isFetching} aria-busy={sessions.isFetching || undefined} onClick={() => sessions.refetch()}>{sessions.isFetching ? "正在重试…" : "会话读取失败，重试"}</button>}
          {sessions.data?.length === 0 && <span className="sidebar-note">还没有审阅记录</span>}
          {sessions.data?.map((session) => (
            <NavLink
              key={session.id}
              to={`/assistant/${session.id}`}
              onClick={() => setOpen(false)}
              className={`session-link ${location.pathname.endsWith(session.id) ? "active" : ""}`}
            >
              <span>{session.title || "未命名审阅"}</span>
              <time dateTime={session.last_message_at}>{formatRelativeTime(session.last_message_at)}</time>
            </NavLink>
          ))}
        </div>
        <div className="sidebar-footer">
          <span className="status-dot" />
          <span>同源工作区</span>
          <span className="environment-label">LIVE</span>
        </div>
      </aside>
      <main ref={mainRef} id="main-content" className="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}

function Brand() {
  return (
    <div className="brand" aria-label="DocReview">
      <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
      <span><strong>DOC</strong>REVIEW</span>
    </div>
  );
}
