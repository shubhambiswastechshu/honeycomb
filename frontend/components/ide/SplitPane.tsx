"use client";

/**
 * A vertical split with a draggable divider: editor above, output below.
 *
 * The top pane's height is the state, in pixels, and the bottom takes the
 * rest. Pixels rather than a percentage because the thing people resize for is
 * "show me four more lines of SQL", which is an absolute amount, and because a
 * percentage silently resizes both panes when the window changes.
 *
 * Pointer events, not mouse events, so a drag works on a trackpad, a touch
 * screen and a pen without three code paths. `setPointerCapture` is what keeps
 * the drag alive when the pointer outruns the 6px divider, which it always
 * does.
 *
 * The divider is also a real control: it takes focus and the arrow keys move
 * it. A resize that only exists for people using a mouse is a layout that some
 * people simply cannot change.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface SplitPaneProps {
  top: ReactNode;
  bottom: ReactNode;
  initialTop?: number;
  minTop?: number;
  minBottom?: number;
  label?: string;
}

const KEYBOARD_STEP = 24;

export default function SplitPane({
  top,
  bottom,
  initialTop = 300,
  minTop = 120,
  minBottom = 140,
  label = "Resize the editor",
}: SplitPaneProps) {
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [topHeight, setTopHeight] = useState(initialTop);
  const [dragging, setDragging] = useState(false);

  const clamp = useCallback(
    function clamp(next: number): number {
      const frame = frameRef.current;
      const available = frame === null ? next + minBottom : frame.clientHeight;
      const ceiling = Math.max(minTop, available - minBottom);
      return Math.min(Math.max(next, minTop), ceiling);
    },
    [minTop, minBottom]
  );

  useEffect(
    function keepInsideFrame() {
      // A window that shrinks below the split must not leave the bottom pane
      // at zero height. Re-clamping on resize is the whole fix.
      function onResize(): void {
        setTopHeight(function current(value) {
          return clamp(value);
        });
      }
      window.addEventListener("resize", onResize);
      return function cleanup() {
        window.removeEventListener("resize", onResize);
      };
    },
    [clamp]
  );

  function startDrag(event: React.PointerEvent<HTMLDivElement>): void {
    event.preventDefault();
    (event.target as HTMLElement).setPointerCapture(event.pointerId);
    setDragging(true);
  }

  function onDrag(event: React.PointerEvent<HTMLDivElement>): void {
    if (!dragging) {
      return;
    }
    const frame = frameRef.current;
    if (frame === null) {
      return;
    }
    setTopHeight(clamp(event.clientY - frame.getBoundingClientRect().top));
  }

  function endDrag(event: React.PointerEvent<HTMLDivElement>): void {
    if (!dragging) {
      return;
    }
    (event.target as HTMLElement).releasePointerCapture(event.pointerId);
    setDragging(false);
  }

  function onKey(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setTopHeight(clamp(topHeight - KEYBOARD_STEP));
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setTopHeight(clamp(topHeight + KEYBOARD_STEP));
    }
  }

  return (
    <div className={dragging ? "ide-split ide-dragging" : "ide-split"} ref={frameRef}>
      <div className="ide-split-top" style={{ height: topHeight }}>
        {top}
      </div>
      <div
        className="ide-split-bar"
        role="separator"
        aria-orientation="horizontal"
        aria-label={label}
        aria-valuenow={Math.round(topHeight)}
        tabIndex={0}
        onPointerDown={startDrag}
        onPointerMove={onDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={onKey}
      >
        <span className="ide-split-grip" aria-hidden="true" />
      </div>
      <div className="ide-split-bottom">{bottom}</div>
    </div>
  );
}
