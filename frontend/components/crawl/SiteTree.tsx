"use client";

/**
 * The crawl as folders: each path segment with how many URLs sit under it and
 * how many of those are broken, redirected or kept out of search.
 *
 * Reading a site's structure is mostly spotting the folder that is too big,
 * too deep or full of errors, so every row shows its share of problems as a
 * bar and a click filters the grid to that folder.
 */

import { useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";
import { getSiteTree } from "@/lib/crawlWorkspace";
import type { TreeNode } from "@/lib/crawlWorkspace";
import { num } from "@/components/crawl/format";

function Row({
  node,
  level,
  onFolder,
}: {
  node: TreeNode;
  level: number;
  onFolder: (path: string) => void;
}) {
  const [open, setOpen] = useState(level === 0);
  const hasChildren = node.children.length > 0;
  const bad = node.pages ? (100 * node.errors) / node.pages : 0;
  const redirected = node.pages ? (100 * node.redirects) / node.pages : 0;

  return (
    <li>
      <div className="tree-row" style={{ paddingLeft: 6 + level * 12 }}>
        {hasChildren ? (
          <button
            type="button"
            className={open ? "tree-toggle is-open" : "tree-toggle"}
            aria-expanded={open}
            aria-label={(open ? "Collapse " : "Expand ") + node.name}
            onClick={() => setOpen((v) => !v)}
          >
            <ChevronRight size={13} aria-hidden="true" />
          </button>
        ) : (
          <span className="tree-toggle" aria-hidden="true" />
        )}
        <button
          type="button"
          className="tree-name"
          title={"Show URLs under " + node.path}
          onClick={() => onFolder(level === 0 ? "" : node.path)}
        >
          {level === 0 ? node.name : "/" + node.name}
        </button>
        <span className="tree-bar" aria-hidden="true">
          <span className="is-bad" style={{ width: bad + "%" }} />
          <span className="is-redirect" style={{ width: redirected + "%" }} />
        </span>
        <span className="tree-n" title={num(node.errors) + " errors, " + num(node.redirects) + " redirects"}>
          {num(node.pages)}
        </span>
      </div>
      {open && hasChildren ? (
        <ul className="tree-list">
          {node.children.map((child) => (
            <Row key={child.path} node={child} level={level + 1} onFolder={onFolder} />
          ))}
          {node.more > 0 ? (
            <li className="tree-more" style={{ paddingLeft: 30 + level * 12 }}>
              and {num(node.more)} more folders
            </li>
          ) : null}
        </ul>
      ) : null}
    </li>
  );
}

export default function SiteTree({ jobId, onFolder }: { jobId: number; onFolder: (path: string) => void }) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(
    function load() {
      let cancelled = false;
      getSiteTree(jobId)
        .then((r) => {
          if (!cancelled) setTree(r.tree);
        })
        .catch((caught) => {
          if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not load the site tree.");
        });
      return function stop() {
        cancelled = true;
      };
    },
    [jobId],
  );

  if (error) return <p className="cr-error">{error}</p>;
  if (!tree) return <p className="ins-empty">Loading…</p>;
  return (
    <div className="tree">
      <p className="ins-note">
        URLs per folder. Red is the share of errors, amber of redirects. Click a folder to list its URLs.
      </p>
      <ul className="tree-list">
        <Row node={tree} level={0} onFolder={onFolder} />
      </ul>
    </div>
  );
}
