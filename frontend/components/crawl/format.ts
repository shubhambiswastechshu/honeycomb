/** Cell and value formatting shared by the grid, the URL pane and the sidebar. */

import type { ColumnType } from "@/lib/crawlWorkspace";

export function num(n: unknown): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString("en-GB") : "—";
}

export function bytes(n: unknown): string {
  if (typeof n !== "number") return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(2) + " MB";
}

export function codeClass(code: unknown): string {
  if (typeof code !== "number") return "cr-code is-none";
  if (code < 300) return "cr-code is-ok";
  if (code < 400) return "cr-code is-redirect";
  if (code < 500) return "cr-code is-client";
  return "cr-code is-server";
}

export function isIndexable(value: unknown): boolean {
  return typeof value === "string" && value.toLowerCase() === "indexable";
}

export function pathOf(url: string): string {
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return url;
  }
}

/** Plain text for a value, used for CSV-like copying and titles. */
export function text(value: unknown, type: ColumnType): string {
  if (type === "bool") return value ? "Yes" : "No";
  if (value === null || value === undefined || value === "") return "";
  switch (type) {
    case "int":
      return num(value);
    case "float":
      return typeof value === "number" ? String(Math.round(value * 100) / 100) : String(value);
    case "ms":
      return typeof value === "number" ? num(value) + " ms" : "";
    case "bytes":
      return bytes(value);
    case "list":
      return Array.isArray(value) ? value.filter((v) => v !== 0 && v !== "0").join(", ") : String(value);
    default:
      return String(value);
  }
}
