import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";

type PageHeaderProps = {
  eyebrow: string;
  title: string;
  actions?: ReactNode;
  backTo?: string;
  backLabel?: string;
};

export function PageHeader({ eyebrow, title, actions, backTo, backLabel = "返回" }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div className="page-header-main">
        {backTo && <Link className="page-back-link" to={backTo} aria-label={backLabel}><ArrowLeft aria-hidden="true" /><span>{backLabel}</span></Link>}
        <div className="page-header-title"><span className="eyebrow">{eyebrow}</span><h1 tabIndex={-1} title={title}>{title}</h1></div>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}
