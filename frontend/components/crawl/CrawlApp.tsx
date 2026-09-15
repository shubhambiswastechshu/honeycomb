"use client";

/**
 * The public crawler: start a crawl, watch it run, read what it found.
 *
 * Laid out like the desktop crawlers people already know -- a queue on the
 * left, the selected crawl on the right, a results grid filtered by status
 * code -- because the job is the same and relearning a layout is friction.
 *
 * Everything here is live by polling, and the polling is throttled to what the
 * server's public read limit allows: the list every 4s, the selected crawl
 * every 2s while it runs, its results every 5s. A finished crawl stops polling.
 *
 * No login. A crawl you start is remembered in this browser (localStorage),
 * with the token that lets only this browser stop it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Download, Loader2, Play, Square } from "lucide-react";
import Workspace from "@/components/crawl/Workspace";
import { LogoMark } from "@/components/ui/Logo";
import {
  CrawlApiError,
  cancelPublicCrawl,
  getPublicCrawl,
  isActive,
  listPublicCrawls,
  publicExportUrl,
  startPublicCrawl,
} from "@/lib/crawl";
import type { CrawlEventLine, Overview, PublicJob } from "@/lib/crawl";

const PAGE_SIZES = [50, 100, 200, 500];
/** The select's value for "Whole site"; sent to the server as "all". */
const WHOLE_SITE = 0;

const MAX_LOG = 300;
const STORE_KEY = "honeycomb.crawl.mine";


const STATUS_WORD: Record<string, string> = {
  queued: "Queued",
  claimed: "Starting",
  running: "Crawling",
  done: "Finished",
  failed: "Failed",
  cancelled: "Stopped",
};

/* ---------------------------------------------------------------- helpers */

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}


function num(n: number | null | undefined): string {
  return typeof n === "number" ? n.toLocaleString("en-GB") : "—";
}

function bytes(n: number): string {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}


function codeClass(code: number | null): string {
  if (code === null) return "cr-code is-none";
  if (code < 300) return "cr-code is-ok";
  if (code < 400) return "cr-code is-redirect";
  if (code < 500) return "cr-code is-client";
  return "cr-code is-server";
}

function progress(job: PublicJob): number {
  if (job.status === "done") return 100;
  // A whole-site crawl's max_pages is a safety ceiling in the tens of
  // thousands, so measuring against it would show a bar that never moves.
  // Crawled against known-so-far is the honest measure there.
  if (job.whole_site) {
    const known = job.pages_crawled + job.pages_queued;
    return known > 0 ? (100 * job.pages_crawled) / known : 0;
  }
  const cap = job.max_pages || 0;
  if (cap > 0) return Math.min(100, (100 * job.pages_crawled) / cap);
  const known = job.pages_crawled + job.pages_queued;
  return known > 0 ? (100 * job.pages_crawled) / known : 0;
}

/** Crawls started in this browser, and the token that lets it stop each one. */
function loadMine(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveMine(mine: Record<string, string>): void {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(mine));
  } catch {
    /* private mode or storage blocked: the crawl still runs, it just cannot be stopped from here later */
  }
}

/* ------------------------------------------------------------------- app */

export default function CrawlApp() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const selectedId = Number(params.get("job")) || null;

  const [overview, setOverview] = useState<Overview | null>(null);
  const [jobs, setJobs] = useState<PublicJob[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [mine, setMine] = useState<Record<string, string>>({});

  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(200);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(function readMine() {
    setMine(loadMine());
  }, []);

  const select = useCallback(
    function select(id: number | null) {
      router.replace(id === null ? pathname : pathname + "?job=" + id, { scroll: false });
    },
    [router, pathname],
  );

  const refreshList = useCallback(async function refreshList() {
    try {
      const data = await listPublicCrawls();
      setOverview(data.overview);
      setJobs(data.jobs);
      setListError(null);
    } catch (caught) {
      if (caught instanceof CrawlApiError && caught.status === 404) {
        setDisabled(true);
      } else {
        setListError(caught instanceof Error ? caught.message : "Could not load crawls.");
      }
    }
  }, []);

  useEffect(
    function pollList() {
      void refreshList();
      const id = window.setInterval(refreshList, 4000);
      return function stop() {
        window.clearInterval(id);
      };
    },
    [refreshList],
  );

  async function onStart(event: FormEvent) {
    event.preventDefault();
    if (starting || url.trim() === "") return;
    setStarting(true);
    setStartError(null);
    try {
      const res = await startPublicCrawl(url.trim(), maxPages === WHOLE_SITE ? "all" : maxPages);
      if (res.cancel_token) {
        const next = { ...loadMine(), [String(res.job.id)]: res.cancel_token };
        saveMine(next);
        setMine(next);
      }
      setUrl("");
      select(res.job.id);
      void refreshList();
    } catch (caught) {
      setStartError(caught instanceof Error ? caught.message : "The crawl could not be started.");
    } finally {
      setStarting(false);
    }
  }

  const selected = useMemo(
    function find() {
      return (jobs || []).find(function match(j) {
        return j.id === selectedId;
      });
    },
    [jobs, selectedId],
  );

  if (disabled) {
    return (
      <div className="cr">
        <TopBar overview={null} />
        <main className="cr-off">
          <h1>The public crawler is switched off</h1>
          <p>This Honeycomb has not enabled crawling without an account.</p>
          <Link className="cr-btn" href="/signin">
            Sign in instead
          </Link>
        </main>
      </div>
    );
  }

  return (
    <div className="cr">
      <TopBar overview={overview} />

      <section className="cr-start" aria-labelledby="cr-start-title">
        <h1 id="cr-start-title" className="cr-start-title">
          Crawl a website
        </h1>
        <form className="cr-form" onSubmit={onStart}>
          <label className="cr-url">
            <span className="cr-sr">Website address</span>
            <input
              type="text"
              inputMode="url"
              autoComplete="url"
              spellCheck={false}
              placeholder="https://example.com"
              value={url}
              disabled={starting}
              onChange={function onChange(e) {
                setUrl(e.target.value);
              }}
            />
          </label>
          <label className="cr-size">
            <span>Up to</span>
            <select
              value={maxPages}
              disabled={starting}
              onChange={function onChange(e) {
                setMaxPages(Number(e.target.value));
              }}
            >
              {PAGE_SIZES.filter(function allowed(n) {
                return !overview || n <= overview.limits.max_pages;
              }).map(function opt(n) {
                return (
                  <option key={n} value={n}>
                    {n} pages
                  </option>
                );
              })}
              <option value={WHOLE_SITE}>Whole site</option>
            </select>
          </label>
          <button className="cr-btn cr-btn-go" type="submit" disabled={starting || url.trim() === ""}>
            {starting ? <Loader2 size={15} className="cr-spin" aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
            {starting ? "Starting…" : "Start crawl"}
          </button>
        </form>
        {startError ? (
          <p className="cr-error" role="alert">
            {startError}
          </p>
        ) : null}
        {overview ? (
          <p className="cr-fine">
            This site only: no subdomains, and no request is ever sent to another site · respects
            robots.txt · whole site up to {num(overview.limits.whole_site_max)} pages ·{" "}
            {overview.limits.requests_per_second} requests a second
          </p>
        ) : null}
        {overview && overview.workers_online === 0 ? (
          <p className="cr-warn" role="status">
            No crawler machine is online right now. Crawls you start will wait in the queue until one connects.
          </p>
        ) : null}
      </section>

      <div className="cr-body">
        <aside className="cr-queue" aria-label="Crawls">
          <div className="cr-queue-head">
            <h2>Crawls</h2>
            {overview && overview.private_crawls_active > 0 ? (
              <span className="cr-muted">
                +{overview.private_crawls_active} private {overview.private_crawls_active === 1 ? "crawl" : "crawls"} running
              </span>
            ) : null}
          </div>
          {listError ? <p className="cr-error">{listError}</p> : null}
          {jobs === null && !listError ? <p className="cr-muted cr-pad">Loading…</p> : null}
          {jobs !== null && jobs.length === 0 ? (
            <p className="cr-muted cr-pad">No crawls yet. Start one above.</p>
          ) : null}
          <ul className="cr-queue-list">
            {(jobs || []).map(function row(job) {
              const active = isActive(job.status);
              return (
                <li key={job.id}>
                  <button
                    type="button"
                    className={job.id === selectedId ? "cr-job is-selected" : "cr-job"}
                    aria-current={job.id === selectedId ? "true" : undefined}
                    onClick={function pick() {
                      select(job.id);
                    }}
                  >
                    <span className={"cr-dot is-" + job.status} aria-hidden="true" />
                    <span className="cr-job-main">
                      <span className="cr-job-host">
                        {hostOf(job.seed_url)}
                        {mine[String(job.id)] ? <span className="cr-mine">yours</span> : null}
                      </span>
                      <span className="cr-job-meta">
                        {STATUS_WORD[job.status] || job.status}
                        {job.status === "queued" && typeof job.queue_position === "number"
                          ? " · " + (job.queue_position === 0 ? "next" : job.queue_position + " ahead")
                          : " · " +
                            num(job.pages_crawled) +
                            (job.whole_site ? " pages · whole site" : job.max_pages ? " / " + num(job.max_pages) + " pages" : " pages")}
                      </span>
                      {active ? (
                        <span className="cr-bar" aria-hidden="true">
                          <span style={{ width: progress(job) + "%" }} />
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </aside>

        <main className="cr-detail">
          {selectedId === null ? (
            <div className="cr-empty">
              <p>Pick a crawl on the left, or start one above, to see its pages as they are found.</p>
            </div>
          ) : (
            <JobView
              key={selectedId}
              jobId={selectedId}
              fromList={selected}
              cancelToken={mine[String(selectedId)] || null}
              onChanged={refreshList}
              compareWith={(jobs || []).filter(function sameSite(j) {
                return (
                  j.id !== selectedId &&
                  j.status === "done" &&
                  selected !== undefined &&
                  hostOf(j.seed_url) === hostOf(selected.seed_url)
                );
              })}
            />
          )}
        </main>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- top bar */

function TopBar({ overview }: { overview: Overview | null }) {
  const online = overview ? overview.workers_online : null;
  return (
    <header className="cr-top">
      <Link href="/crawl" className="cr-brand">
        <LogoMark size={22} />
        <span>Honeycomb</span>
        <span className="cr-brand-sub">Site crawler</span>
      </Link>
      <div className="cr-top-right">
        {online !== null ? (
          <span className={online > 0 ? "cr-pill is-on" : "cr-pill"} role="status">
            <span className="cr-pill-dot" aria-hidden="true" />
            {online > 0 ? online + (online === 1 ? " crawler online" : " crawlers online") : "No crawler online"}
          </span>
        ) : null}
        <Link href="/signin" className="cr-top-link">
          Sign in
        </Link>
      </div>
    </header>
  );
}

/* --------------------------------------------------------------- job view */

function JobView({
  jobId,
  fromList,
  cancelToken,
  onChanged,
  compareWith,
}: {
  jobId: number;
  fromList: PublicJob | undefined;
  cancelToken: string | null;
  onChanged: () => void;
  compareWith: PublicJob[];
}) {
  const [job, setJob] = useState<PublicJob | null>(fromList || null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<CrawlEventLine[]>([]);
  const [tab, setTab] = useState<"pages" | "console">("pages");
  const [stopping, setStopping] = useState(false);
  const since = useRef(0);

  const active = job ? isActive(job.status) : true;

  const refresh = useCallback(
    async function refresh() {
      try {
        const data = await getPublicCrawl(jobId, since.current);
        setJob(data.job);
        setError(null);
        if (data.events.length > 0) {
          since.current = data.events[data.events.length - 1].seq;
          setLog(function append(prev) {
            return prev.concat(data.events).slice(-MAX_LOG);
          });
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not load this crawl.");
      }
    },
    [jobId],
  );

  useEffect(
    function poll() {
      void refresh();
      if (!active) return;
      const id = window.setInterval(refresh, 2000);
      return function stop() {
        window.clearInterval(id);
      };
    },
    [refresh, active],
  );

  async function stop() {
    if (!cancelToken || stopping) return;
    setStopping(true);
    try {
      const data = await cancelPublicCrawl(jobId, cancelToken);
      setJob(data.job);
      onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not stop the crawl.");
    } finally {
      setStopping(false);
    }
  }

  if (error && !job) {
    return <p className="cr-error cr-pad">{error}</p>;
  }
  if (!job) {
    return <p className="cr-muted cr-pad">Loading…</p>;
  }

  return (
    <div className="cr-job-view">
      <div className="cr-head">
        <div className="cr-head-main">
          <h2 className="cr-head-host">{hostOf(job.seed_url)}</h2>
          <p className="cr-head-meta" aria-live="polite">
            <span className={"cr-dot is-" + job.status} aria-hidden="true" />
            {STATUS_WORD[job.status] || job.status}
            {job.cancel_requested && active ? " · stopping" : ""}
            {job.status === "queued" && typeof job.queue_position === "number"
              ? " · " + (job.queue_position === 0 ? "starts next" : job.queue_position + " crawls ahead")
              : ""}
            <span className="cr-head-url">{job.seed_url}</span>
          </p>
        </div>
        <div className="cr-head-actions">
          {cancelToken && active && !job.cancel_requested ? (
            <button type="button" className="cr-btn cr-btn-ghost" onClick={stop} disabled={stopping}>
              <Square size={13} aria-hidden="true" />
              {stopping ? "Stopping…" : "Stop"}
            </button>
          ) : null}
          <a className="cr-btn cr-btn-ghost" href={publicExportUrl(job.id)} download>
            <Download size={14} aria-hidden="true" />
            Export CSV
          </a>
        </div>
      </div>

      <dl className="cr-stats">
        <Stat
          label={job.whole_site ? "Crawled · whole site" : "Crawled"}
          value={num(job.pages_crawled) + (!job.whole_site && job.max_pages ? " / " + num(job.max_pages) : "")}
        />
        <Stat label="Discovered" value={num(job.urls_discovered)} />
        <Stat label="Links" value={num(job.links_found)} />
        <Stat label="Failures" value={num(job.failures)} tone={job.failures > 0 ? "bad" : undefined} />
        <Stat label="Speed" value={job.rate.toFixed(1) + " /s"} />
        <Stat label="Elapsed" value={clock(job.duration_seconds)} />
        <Stat label="Downloaded" value={bytes(job.bytes_downloaded)} />
      </dl>
      <div className="cr-bar cr-bar-lg" aria-hidden="true">
        <span style={{ width: progress(job) + "%" }} />
      </div>

      <div className="cr-tabs" role="tablist" aria-label="Crawl views">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "pages"}
          className={tab === "pages" ? "cr-tab is-on" : "cr-tab"}
          onClick={function show() {
            setTab("pages");
          }}
        >
          Workspace
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "console"}
          className={tab === "console" ? "cr-tab is-on" : "cr-tab"}
          onClick={function show() {
            setTab("console");
          }}
        >
          Live log
        </button>
      </div>

      {tab === "pages" ? (
        <Workspace jobId={job.id} live={active} compareWith={compareWith} />
      ) : (
        <Console lines={log} live={active} />
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "bad" }) {
  return (
    <div className={tone === "bad" ? "cr-stat is-bad" : "cr-stat"}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/* --------------------------------------------------------------- console */

function Console({ lines, live }: { lines: CrawlEventLine[]; live: boolean }) {
  const end = useRef<HTMLDivElement | null>(null);
  useEffect(
    function follow() {
      end.current?.scrollIntoView({ block: "nearest" });
    },
    [lines.length],
  );
  return (
    <div className="cr-console" role="log" aria-live="off">
      {lines.length === 0 ? (
        <p className="cr-console-line cr-muted">{live ? "Waiting for the crawler…" : "No log lines."}</p>
      ) : null}
      {lines.map(function line(l) {
        return (
          <p key={l.seq} className={"cr-console-line is-" + l.level}>
            {l.status_code !== null ? <span className={codeClass(l.status_code)}>{l.status_code}</span> : null}
            <span className="cr-console-text">{l.text}</span>
            {l.duration_ms !== null ? <span className="cr-console-ms">{l.duration_ms} ms</span> : null}
          </p>
        );
      })}
      <div ref={end} />
    </div>
  );
}
