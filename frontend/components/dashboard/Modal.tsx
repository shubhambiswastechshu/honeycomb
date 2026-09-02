"use client";

/**
 * The app's modal shell: the blurred backdrop, the focus trap, and the exit
 * animation. Every dialog in the dashboard is one of these.
 *
 * Extracted from ConfirmDialog when the connect form needed the same
 * treatment. The alternative was a second copy of the focus-trapping and the
 * timer that keeps the dialog mounted through its exit -- and two copies of
 * that drift, which means one of them eventually loses the Escape key or
 * leaves focus stranded on an element that no longer exists.
 *
 * What it guarantees, none of which a plain absolutely-positioned div does:
 *
 *   1. Focus moves in on open and returns to whatever opened it on close.
 *   2. Tab is trapped between the dialog's own controls.
 *   3. Escape and a backdrop click both dismiss -- never confirm.
 *   4. It stays mounted for the length of the exit animation, because the
 *      backdrop blurs the page and un-blurring has to be seen to be smooth.
 *   5. It renders into document.body, NOT where it is written.
 *
 * The portal host carries `dash` as well. Almost every class the dashboard
 * uses is scoped `.dash .thing`, so a dialog portaled to a bare body loses all
 * of it -- the connect form's Cancel button came out as an unstyled browser
 * default. `display: contents` on the host removes its own box, so it grants
 * the scope without bringing `.dash`'s full-height flex layout with it.
 *
 * That portal is not tidiness. `position: fixed` is relative to the viewport
 * only until some ancestor establishes a containing block, and a transform --
 * including one left behind by a finished animation with fill-mode `both` --
 * is enough to do it. `.dash .panel` runs exactly such an animation, so a
 * backdrop rendered inside it was being clipped to the panel: the page dimmed
 * and blurred in a rectangle around the content and stayed bright everywhere
 * else. A portal puts the dialog outside every one of those ancestors, so no
 * future transform anywhere in the tree can break it again.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { ReactNode } from "react";

/**
 * Lets a control inside the dialog close it the same way Escape does -- through
 * the exit animation rather than round it.
 *
 * A Cancel button that calls the parent's onClose directly unmounts the dialog
 * on the click, and the page snaps back into focus with no un-blur. Same
 * dialog, two different exits, depending on which way you dismissed it.
 */
const DismissContext = createContext<(() => void) | null>(null);

/** Dismiss the enclosing Modal. A no-op outside one. */
export function useModalDismiss(): () => void {
  const dismiss = useContext(DismissContext);
  return dismiss ?? function noop() {};
}

/** Must match the .is-closing animation in dashboard.css. */
const EXIT_MS = 160;

/** Everything inside the dialog that a Tab can land on. */
const FOCUSABLE =
  'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

export interface ModalProps {
  open: boolean;
  /** Called once the exit animation has finished, never before. */
  onClose: () => void;
  children: ReactNode;
  /** id of the element naming the dialog, for aria-labelledby. */
  labelledBy?: string;
  /** id of the element describing it, for aria-describedby. */
  describedBy?: string;
  /**
   * Blocks dismissal. Set while a request the dialog started is still in
   * flight: closing then would hide something that is still happening.
   */
  busy?: boolean;
  /** Extra classes on the panel, e.g. a width modifier. */
  panelClassName?: string;
  /**
   * Shows a close cross in the panel's top corner. For a dialog whose own
   * buttons already offer a way out -- a confirmation -- it is redundant; for
   * a form, it is the way out.
   */
  showClose?: boolean;
  /**
   * Focused on open. Without one, focus goes to the first focusable control,
   * which is right for a form and wrong for a confirmation -- there the
   * primary action should be under the finger.
   */
  initialFocusRef?: React.RefObject<HTMLElement>;
}

export default function Modal({
  open,
  onClose,
  children,
  labelledBy,
  describedBy,
  busy,
  panelClassName,
  initialFocusRef,
  showClose,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Captured on open so focus can go back exactly where it came from.
  const openerRef = useRef<HTMLElement | null>(null);
  const [closing, setClosing] = useState<boolean>(false);
  const exitTimer = useRef<number | null>(null);
  // document does not exist while the server renders, so the portal can only
  // be created once this is running in the browser.
  const [host, setHost] = useState<HTMLElement | null>(null);

  useEffect(function createHost() {
    const node = document.createElement("div");
    // `dash` for the scoped styles, `hc-modal-host` to strip the layout those
    // styles would otherwise bring with them.
    node.className = "dash hc-modal-host";
    document.body.appendChild(node);
    setHost(node);
    return function removeHost() {
      document.body.removeChild(node);
    };
  }, []);

  const isBusy = busy === true;

  const dismiss = useCallback(
    function dismiss(): void {
      if (isBusy || closing) {
        return;
      }
      setClosing(true);
      exitTimer.current = window.setTimeout(function finish() {
        exitTimer.current = null;
        setClosing(false);
        onClose();
      }, EXIT_MS);
    },
    [isBusy, closing, onClose]
  );

  useEffect(
    function manageFocus() {
      if (!open) {
        return;
      }
      setClosing(false);

      const opener = document.activeElement;
      openerRef.current = opener instanceof HTMLElement ? opener : null;

      const wanted = initialFocusRef?.current ?? null;
      if (wanted !== null) {
        wanted.focus();
      } else {
        const panel = panelRef.current;
        const first =
          panel === null ? null : panel.querySelector<HTMLElement>(FOCUSABLE);
        if (first !== null) {
          first.focus();
        }
      }

      return function restoreFocus() {
        if (exitTimer.current !== null) {
          window.clearTimeout(exitTimer.current);
          exitTimer.current = null;
        }
        const previous = openerRef.current;
        if (previous !== null && document.contains(previous)) {
          previous.focus();
        }
      };
    },
    [open, initialFocusRef]
  );

  useEffect(
    function bindKeys() {
      if (!open) {
        return;
      }

      function onKeyDown(event: KeyboardEvent): void {
        if (event.key === "Escape") {
          event.preventDefault();
          dismiss();
          return;
        }
        if (event.key !== "Tab") {
          return;
        }

        const panel = panelRef.current;
        if (panel === null) {
          return;
        }
        const items = Array.prototype.slice.call(
          panel.querySelectorAll(FOCUSABLE)
        ) as HTMLElement[];
        if (items.length === 0) {
          return;
        }

        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;

        // Wrap by hand: without this, Tab walks out of the dialog and into the
        // page behind it, which is still rendered.
        if (event.shiftKey && (active === first || !panel.contains(active))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }

      window.addEventListener("keydown", onKeyDown, true);
      return function unbind() {
        window.removeEventListener("keydown", onKeyDown, true);
      };
    },
    [open, dismiss]
  );

  if (!open || host === null) {
    return null;
  }

  return createPortal(
    <div
      className={closing ? "hc-modal-backdrop is-closing" : "hc-modal-backdrop"}
      onMouseDown={function onBackdrop(event) {
        // mousedown, and only when it starts on the backdrop itself: a drag
        // that begins on the text inside and ends outside must not dismiss.
        if (event.target === event.currentTarget) {
          dismiss();
        }
      }}
    >
      <div
        className={
          typeof panelClassName === "string"
            ? "hc-modal " + panelClassName
            : "hc-modal"
        }
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
      >
        {showClose === true ? (
          <button
            type="button"
            className="hc-modal-close"
            onClick={dismiss}
            disabled={isBusy}
            aria-label="Close"
          >
            <X size={17} strokeWidth={2} aria-hidden="true" />
          </button>
        ) : null}
        <DismissContext.Provider value={dismiss}>
          {children}
        </DismissContext.Provider>
      </div>
    </div>,
    host
  );
}
