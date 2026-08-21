const statusFormatter = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

export function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : statusFormatter.format(date);
}

export function formatRelativeTime(value: string): string {
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  if (Number.isNaN(seconds)) return "";
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  if (Math.abs(seconds) < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

export function shortId(value?: string): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : "—";
}
