import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { Button, IconButton } from "./Button";

export function ConfirmDialog({ open, onOpenChange, title, detail, confirmLabel, danger, busy, confirmDisabled, onConfirm, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; detail?: string; confirmLabel: string;
  danger?: boolean; busy?: boolean; confirmDisabled?: boolean; onConfirm: () => void; children?: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(nextOpen) => { if (!busy || nextOpen) onOpenChange(nextOpen); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-busy={busy || undefined}>
          <div className="dialog-header"><Dialog.Title>{title}</Dialog.Title><Dialog.Close asChild><IconButton label="关闭" disabled={busy}><X /></IconButton></Dialog.Close></div>
          {detail && <Dialog.Description>{detail}</Dialog.Description>}
          {children}
          <div className="dialog-actions"><Dialog.Close asChild><Button disabled={busy}>取消</Button></Dialog.Close><Button variant={danger ? "danger" : "primary"} disabled={confirmDisabled} loading={busy} loadingLabel="正在提交…" onClick={onConfirm}>{confirmLabel}</Button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
