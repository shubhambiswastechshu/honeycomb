/**
 * Client for the public crawler (Honeycomb/foraging/public.py).
 *
 * Plain fetch, no cookies. These endpoints take no login, and sending the
 * session anyway would only make a signed-in visitor's requests count against
 * their account instead of their address.
 */

import { API_BASE } from "@/lib/api";

const BASE = API_BASE + "/forager/public";

export type CrawlStatus = "queued" | "claimed" | "running" | "paused" | "done" | "failed" | "cancelled";

export type CrawlAction = "stop" | "pause" | "resume";

export interface PublicJob {
  id: number;
  seed_url: string;
  status: CrawlStatus;
  max_pages: number | null;
  /** Started with "Whole site": max_pages is only a safety ceiling, not a target. */
  whole_site: boolean;
  pages_crawled: number;
  pages_queued: number;
  urls_discovered: number;
  links_found: number;
  failures: number;
  bytes_downloaded: number;
  status_counts: Record<string, number>;
  rate: number;
  duration_seconds: number;
  cancel_requested: boolean;
  /** Pause asked for; the worker has not stopped yet. */
  pause_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  queue_position?: number | null;
  settings?: CrawlSettings;
}

/** Options from the settings panel. Rules are plain "contains" text; * is a wildcard. */
export interface CrawlSettings {
  include: string[];
  exclude: string[];
  depth: number | null;
  ignore_params: boolean;
  render: boolean;
}

export interface Overview {
  workers_online: number;
  private_crawls_active: number;
  public_crawls_active: number;
  limits: {
    max_pages: number;
    max_active: number;
    daily: number;
    whole_site_max: number;
    requests_per_second: number;
  };
}

export interface CrawlEventLine {
  seq: number;
  at: string;
  level: string;
  text: string;
  status_code: number | null;
  duration_ms: number | null;
}

export interface PageRow {
  url: string;
  status_code: number | null;
  content_type: string;
  title: string;
  title_length: number;
  meta_description_length: number;
  h1_1: string;
  word_count: number;
  indexability: string;
  indexability_status: string;
  response_time_ms: number | null;
  size_bytes: number;
  depth: number;
  inlinks: number;
  outlinks: number;
  canonical: string;
  final_url: string;
}

export type PageBucket = "all" | "2xx" | "3xx" | "4xx" | "5xx" | "errors" | "noindex";

export interface PagesResponse {
  counts: Record<PageBucket, number>;
  total: number;
  offset: number;
  limit: number;
  results: PageRow[];
}

export class CrawlApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "CrawlApiError";
    this.status = status;
    Object.setPrototypeOf(this, CrawlApiError.prototype);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      credentials: "omit",
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
  } catch {
    throw new CrawlApiError("The crawler could not be reached. Check your connection.", 0);
  }
  const body = await res.json().catch(function none() {
    return {};
  });
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail;
    if (res.status === 429 && !detail) {
      throw new CrawlApiError("Too many requests. Wait a moment and try again.", 429);
    }
    throw new CrawlApiError(detail || "Request failed (" + res.status + ").", res.status);
  }
  return body as T;
}

/** Recent crawls, plus any pinned in this browser that are no longer recent. */
export function listPublicCrawls(pinned: number[] = []): Promise<{ overview: Overview; jobs: PublicJob[] }> {
  return call("/jobs/" + (pinned.length ? "?pinned=" + pinned.join(",") : ""));
}

/** Delete a crawl and its results. Needs this browser's token; refused while it runs. */
export function deletePublicCrawl(id: number, token: string): Promise<{ deleted: number }> {
  return call("/jobs/" + id + "/delete/", { method: "POST", body: JSON.stringify({ cancel_token: token }) });
}

/** "all" asks for the whole site, up to the server's safety ceiling. */
export function startPublicCrawl(
  url: string,
  maxPages: number | "all",
  settings?: Partial<CrawlSettings>,
): Promise<{ job: PublicJob; cancel_token?: string; existing?: boolean }> {
  return call("/jobs/", {
    method: "POST",
    body: JSON.stringify({ ...(settings || {}), url: url, max_pages: maxPages }),
  });
}

export function getPublicCrawl(
  id: number,
  since: number,
): Promise<{ job: PublicJob; events: CrawlEventLine[] }> {
  return call("/jobs/" + id + "/?since=" + since);
}

export function getPublicPages(
  id: number,
  opts: { status: PageBucket; q: string; offset: number; limit: number },
): Promise<PagesResponse> {
  const params = new URLSearchParams({
    status: opts.status,
    q: opts.q,
    offset: String(opts.offset),
    limit: String(opts.limit),
  });
  return call("/jobs/" + id + "/pages/?" + params.toString());
}

export function cancelPublicCrawl(id: number, token: string): Promise<{ job: PublicJob }> {
  return call("/jobs/" + id + "/cancel/", { method: "POST", body: JSON.stringify({ cancel_token: token }) });
}

/** Stop, pause or resume. Needs the token this browser got when it started the crawl. */
export function controlPublicCrawl(id: number, action: CrawlAction, token: string): Promise<{ job: PublicJob }> {
  return call("/jobs/" + id + "/control/", {
    method: "POST",
    body: JSON.stringify({ action: action, cancel_token: token }),
  });
}

export function publicExportUrl(id: number): string {
  return BASE + "/jobs/" + id + "/export.csv";
}

export function isActive(status: CrawlStatus): boolean {
  return status === "queued" || status === "claimed" || status === "running";
}
