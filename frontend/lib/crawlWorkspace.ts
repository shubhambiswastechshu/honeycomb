/**
 * Client for the crawler workspace (Honeycomb/foraging/public_workspace.py).
 *
 * The server owns the tabs, their columns and their filters, so this file only
 * describes shapes. Adding a filter on the server shows up here without a
 * frontend change.
 */

import { API_BASE } from "@/lib/api";
import { CrawlApiError } from "@/lib/crawl";
import type { PublicJob } from "@/lib/crawl";

const BASE = API_BASE + "/forager/public/jobs/";

export type ColumnType = "url" | "text" | "int" | "float" | "ms" | "bytes" | "code" | "list" | "index";
export type Severity = "high" | "medium" | "low";

export interface GridColumn {
  key: string;
  label: string;
  type: ColumnType;
}

export interface TabFilter {
  key: string;
  label: string;
  count: number;
}

export interface WorkspaceTab {
  key: string;
  label: string;
  filters: TabFilter[];
}

export interface IssueRow {
  code: string;
  label: string;
  severity: Severity;
  pages: number;
}

export interface Workspace {
  job: PublicJob;
  pages_total: number;
  tabs: WorkspaceTab[];
  issues: IssueRow[];
  issue_totals: Record<Severity, number>;
  structure: { depth: number; pages: number }[];
  response_times: { bucket: string; pages: number }[];
  status_codes: { code: number | null; pages: number }[];
  indexability: { reason: string; indexable: boolean; pages: number }[];
  content_types: { type: string; pages: number }[];
  thresholds: Record<string, number>;
}

export type GridRow = Record<string, unknown>;

export interface GridResponse {
  tab: string;
  filter: string;
  issue: string;
  issue_label: string;
  columns: GridColumn[];
  sort: string;
  dir: "asc" | "desc";
  total: number;
  offset: number;
  limit: number;
  rows: GridRow[];
}

export interface GridQuery {
  tab: string;
  filter: string;
  issue: string;
  q: string;
  sort: string;
  dir: "" | "asc" | "desc";
  limit: number;
}

export interface UrlDetail {
  url: string;
  fields: { key: string; label: string; type: ColumnType; value: unknown }[];
  issues: { code: string; label: string; severity: Severity }[];
  content_hash: string | null;
  serp: {
    url: string;
    title: string;
    title_pixel_width: number;
    title_truncated: boolean;
    meta: string;
    meta_pixel_width: number;
    meta_truncated: boolean;
    title_max_px: number;
    meta_max_px: number;
  };
}

export interface ReportSummary {
  name: string;
  title: string;
  description: string;
}

export interface ReportResult extends ReportSummary {
  columns: string[];
  rows: unknown[][];
  count: number;
  truncated: boolean;
}

export interface CompareResult {
  current: PublicJob;
  previous: PublicJob;
  summary: { added: number; removed: number; status_changed: number; title_changed: number; in_both: number };
  added: string[];
  removed: string[];
  status_changed: { url: string; was: number | null; now: number | null }[];
  title_changed: { url: string; was: string; now: string }[];
  truncated: boolean;
}

async function call<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { credentials: "omit" });
  } catch {
    throw new CrawlApiError("The crawler could not be reached. Check your connection.", 0);
  }
  const body = await res.json().catch(function none() {
    return {};
  });
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail;
    throw new CrawlApiError(detail || "Request failed (" + res.status + ").", res.status);
  }
  return body as T;
}

function gridParams(query: GridQuery, offset: number): string {
  const params = new URLSearchParams({
    tab: query.tab,
    filter: query.filter,
    offset: String(offset),
    limit: String(query.limit),
  });
  if (query.issue) params.set("issue", query.issue);
  if (query.q) params.set("q", query.q);
  if (query.sort) params.set("sort", query.sort);
  if (query.dir) params.set("dir", query.dir);
  return params.toString();
}

export function getWorkspace(jobId: number): Promise<Workspace> {
  return call(jobId + "/workspace/");
}

export function getGrid(jobId: number, query: GridQuery): Promise<GridResponse> {
  return call(jobId + "/grid/?" + gridParams(query, 0));
}

export function gridExportUrl(jobId: number, query: GridQuery): string {
  return BASE + jobId + "/grid.csv?" + gridParams(query, 0);
}

export function getUrlDetail(jobId: number, url: string): Promise<UrlDetail> {
  return call(jobId + "/url/?u=" + encodeURIComponent(url));
}

export function listReports(jobId: number): Promise<{ reports: ReportSummary[] }> {
  return call(jobId + "/reports/");
}

export function runReport(jobId: number, name: string): Promise<ReportResult> {
  return call(jobId + "/reports/" + encodeURIComponent(name) + "/?limit=200");
}

export function sitemapUrl(jobId: number): string {
  return BASE + jobId + "/sitemap.xml";
}

export function compareCrawls(jobId: number, against: number): Promise<CompareResult> {
  return call(jobId + "/compare/?against=" + against);
}
