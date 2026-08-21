import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import clsx from "clsx";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  loading?: boolean;
  loadingLabel?: string;
};

export function Button({ className, variant = "secondary", children, disabled, loading = false, loadingLabel, ...props }: ButtonProps) {
  return <button className={clsx("button", `button-${variant}`, loading && "is-loading", className)} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>{loading && <LoaderCircle className="spin" aria-hidden="true" />}{loading && loadingLabel ? loadingLabel : children}</button>;
}

export function IconButton({ label, className, children, disabled, loading = false, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode; loading?: boolean }) {
  return <button className={clsx("icon-button", loading && "is-loading", className)} aria-label={label} title={label} disabled={disabled || loading} aria-busy={loading || undefined} {...props}><span aria-hidden="true">{loading ? <LoaderCircle className="spin" /> : children}</span></button>;
}
