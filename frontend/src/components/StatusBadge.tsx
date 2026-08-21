import { CircleCheck, CircleDashed, CirclePause, CircleX, Clock3, ShieldAlert } from "lucide-react";

const labels: Record<string, string> = {
  queued: "排队中", running: "运行中", waiting_input: "等待输入", waiting_approval: "等待审批",
  succeeded: "已完成", failed: "失败", cancelled: "已取消", pending: "待审批", approved: "已批准",
  rejected: "已拒绝", completed: "已完成",
};

export function StatusBadge({ status }: { status: string }) {
  const Icon = status === "succeeded" || status === "approved" || status === "completed" ? CircleCheck
    : status === "failed" || status === "rejected" || status === "cancelled" ? CircleX
      : status === "waiting_approval" || status === "pending" ? ShieldAlert
        : status === "waiting_input" ? CirclePause
          : status === "running" ? CircleDashed : Clock3;
  return <span className={`status-badge status-${status}`}><Icon aria-hidden="true" />{labels[status] || status}</span>;
}
