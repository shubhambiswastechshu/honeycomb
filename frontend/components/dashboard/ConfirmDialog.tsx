"use client";

/**
 * A confirmation dialog owned by this app, not by the browser.
 *
 * `window.confirm` is deliberately not used: it cannot be styled, it blocks
 * the main thread, and its wording is chosen by the browser rather than by the
 * product.
 *
 * The backdrop, the focus trap, the Escape key and the exit animation all come
 * from Modal. What is left here is the part that is actually a *confirmation*:
 * a question, and two buttons where the safe answer is the one a stray key
 * gives.
 */

import { useRef } from "react";
import type { ReactNode } from "react";
import Modal, { useModalDismiss } from "@/components/dashboard/Modal";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** Body copy. A node rather than a string so callers can emphasise a name. */
  description: ReactNode;
  confirmLabel: string;
  /** Shown on the confirm button while `pending` is true. */
  pendingLabel?: string;
  cancelLabel?: string;
  /** Tints the confirm button as a destructive action. */
  destructive?: boolean;
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Split out so it can use the dismiss hook, which only works inside the Modal
 * that provides it -- ConfirmDialog itself renders outside that boundary.
 */
function CancelButton({ label, disabled }: { label: string; disabled: boolean }) {
  const dismiss = useModalDismiss();
  return (
    <button
      type="button"
      className="hc-modal-cancel"
      onClick={dismiss}
      disabled={disabled}
    >
      {label}
    </button>
  );
}

export default function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  pendingLabel,
  cancelLabel,
  destructive,
  pending,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const isPending = pending === true;

  return (
    <Modal
      open={open}
      onClose={onCancel}
      busy={isPending}
      labelledBy="hc-modal-title"
      describedBy="hc-modal-text"
      // The confirm button, not the cancel one: it is what the dialog is
      // asking about, and cancel is always one Escape away regardless.
      initialFocusRef={confirmRef}
    >
      <h2 className="hc-modal-title" id="hc-modal-title">
        {title}
      </h2>
      <div className="hc-modal-text" id="hc-modal-text">
        {description}
      </div>

      <div className="hc-modal-actions">
        <CancelButton
          label={typeof cancelLabel === "string" ? cancelLabel : "Cancel"}
          disabled={isPending}
        />
        <button
          type="button"
          ref={confirmRef}
          className={
            destructive === true
              ? "hc-modal-confirm hc-modal-confirm-danger"
              : "hc-modal-confirm"
          }
          onClick={function onConfirmClick() {
            if (!isPending) {
              onConfirm();
            }
          }}
          disabled={isPending}
        >
          {isPending && typeof pendingLabel === "string"
            ? pendingLabel
            : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
