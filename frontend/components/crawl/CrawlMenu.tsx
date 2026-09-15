"use client";

/**
 * The menu for one crawl in the Crawls list: right-click a crawl, or use its
 * "more" button, which is the same menu for keyboards and touch screens.
 *
 * Pause, resume and stop only work in the browser that started the crawl (it
 * holds the token), so elsewhere they are shown disabled with the reason,
 * rather than hidden -- a missing option reads as a missing feature.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { Copy, Download, ExternalLink, FolderOpen, Pause, Play, RotateCw, Square } from "lucide-react";
import { isActive } from "@/lib/crawl";
import type { PublicJob } from "@/lib/crawl";

export type MenuAction = "open" | "pause" | "resume" | "stop" | "again" | "visit" | "copy" | "export";

interface Item {
  action: MenuAction;
  label: string;
  icon: ReactNode;
  disabled?: boolean;
  danger?: boolean;
}

export default function CrawlMenu({
  job,
  x,
  y,
  canControl,
  onAction,
  onClose,
}: {
  job: PublicJob;
  x: number;
  y: number;
  canControl: boolean;
  onAction: (action: MenuAction) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ left: x, top: y });

  const active = isActive(job.status);
  const paused = job.status === "paused";
  const pausing = active && job.pause_requested;
  const locked = !canControl;

  const controls: Item[] = [];
  if (!job.cancel_requested) {
    if (paused || pausing) {
      controls.push({ action: "resume", label: pausing ? "Keep crawling" : "Resume crawl", icon: <Play size={14} />, disabled: locked });
    } else if (active) {
      controls.push({ action: "pause", label: "Pause crawl", icon: <Pause size={14} />, disabled: locked });
    }
    if (active || paused) {
      controls.push({ action: "stop", label: "Stop crawl…", icon: <Square size={14} />, disabled: locked, danger: true });
    }
  }

  const opening: Item[] = [{ action: "open", label: "Open crawl", icon: <FolderOpen size={14} /> }];
  const everyone: Item[] = [
    { action: "again", label: "Crawl again", icon: <RotateCw size={14} /> },
    { action: "visit", label: "Open website", icon: <ExternalLink size={14} /> },
    { action: "copy", label: "Copy address", icon: <Copy size={14} /> },
    { action: "export", label: "Export CSV", icon: <Download size={14} /> },
  ];
  const groups: Item[][] = [opening, controls, everyone].filter((g) => g.length > 0);
  const showLockNote = locked && groups.some((g) => g.some((i) => i.action === "pause" || i.action === "resume" || i.action === "stop"));

  // Keep the whole menu on screen, however near an edge the click was.
  useLayoutEffect(
    function clamp() {
      const el = ref.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      setPos({
        left: Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)),
        top: Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)),
      });
    },
    [x, y],
  );

  useEffect(
    function focusAndDismiss() {
      const first = ref.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
      first?.focus();
      function outside(e: MouseEvent) {
        if (ref.current && !ref.current.contains(e.target as Node)) onClose();
      }
      function away() {
        onClose();
      }
      document.addEventListener("mousedown", outside);
      window.addEventListener("resize", away);
      window.addEventListener("blur", away);
      document.addEventListener("scroll", away, true);
      return function cleanup() {
        document.removeEventListener("mousedown", outside);
        window.removeEventListener("resize", away);
        window.removeEventListener("blur", away);
        document.removeEventListener("scroll", away, true);
      };
    },
    [onClose],
  );

  function onKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    const items = Array.from(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || []);
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "Escape" || e.key === "Tab") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      items[(at + 1) % items.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items[(at - 1 + items.length) % items.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      items[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      items[items.length - 1]?.focus();
    }
  }

  return (
    <div
      ref={ref}
      className="cr-menu"
      role="menu"
      aria-label={"Actions for " + job.seed_url}
      style={{ left: pos.left, top: pos.top }}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => e.preventDefault()}
    >
      {groups.map(function group(items, gi) {
        return (
          <div key={gi} className="cr-menu-group" role="group">
            {items.map(function item(it) {
              return (
                <button
                  key={it.action}
                  type="button"
                  role="menuitem"
                  className={it.danger ? "cr-menu-item is-danger" : "cr-menu-item"}
                  disabled={it.disabled}
                  onClick={function choose() {
                    onAction(it.action);
                  }}
                >
                  <span className="cr-menu-icon" aria-hidden="true">
                    {it.icon}
                  </span>
                  {it.label}
                </button>
              );
            })}
          </div>
        );
      })}
      {showLockNote ? (
        <p className="cr-menu-note">Only the browser that started this crawl can pause or stop it.</p>
      ) : null}
    </div>
  );
}
