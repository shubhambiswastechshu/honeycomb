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
import { Download, Loader2, MoreVertical, Pause, Pin, Play, SlidersHorizontal, Square } from "lucide-react";
import CrawlMenu from "@/components/crawl/CrawlMenu";
import type { MenuAction } from "@/components/crawl/CrawlMenu";
import Workspace from "@/components/crawl/Workspace";
import { LogoMark } from "@/components/ui/Logo";
import {
  CrawlApiError,
  controlPublicCrawl,
  deletePublicCrawl,
  getPublicCrawl,
  isActive,
  listPublicCrawls,
  publicExportUrl,
  startPublicCrawl,
} from "@/lib/crawl";
import type { CrawlAction, CrawlEventLine, Overview, PublicJob } from "@/lib/crawl";

const PAGE_SIZES = [50, 100, 200, 500];
/** The select's value for "Whole site"; sent to the server as "all". */
const WHOLE_SITE = 0;

const MAX_LOG = 300;
const STORE_KEY = "honeycomb.crawl.mine";
const PIN_KEY = "honeycomb.crawl.pinned";


const STATUS_WORD: Record<string, string> = {
  queued: "Queued",
  claimed: "Starting",
  running: "Crawling",
  paused: "Paused",
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

/** One rule per line or comma, blanks dropped. */
function splitRules(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
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

/** Crawls pinned in this browser, most recently pinned first. */
function loadPins(): number[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(PIN_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((n) => Number.isInteger(n)).slice(0, 50) : [];
  } catch {
    return [];
  }
}

function savePins(pins: number[]): void {
  try {
    window.localStorage.setItem(PIN_KEY, JSON.stringify(pins));
  } catch {
    /* storage blocked: pins last until the page is reloaded */
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
  const [pins, setPins] = useState<number[]>([]);
  // Read by the list poll without restarting its timer on every pin change.
  const pinsRef = useRef<number[]>([]);

  /** Pinned crawls first, in pin order, then everything else as the server sent it. */
  const ordered = useMemo(
    function order() {
      const list = jobs || [];
      const pinnedJobs = pins
        .map((id) => list.find((j) => j.id === id))
        .filter((j): j is PublicJob => j !== undefined);
      return pinnedJobs.concat(list.filter((j) => !pins.includes(j.id)));
    },
    [jobs, pins],
  );
  const [menu, setMenu] = useState<{ job: PublicJob; x: number; y: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Where focus goes back to when the menu closes: the crawl it was opened on.
  const menuReturn = useRef<HTMLElement | null>(null);

  const closeMenu = useCallback(function closeMenu() {
    setMenu(null);
    menuReturn.current?.focus();
  }, []);

  useEffect(
    function hideNotice() {
      if (!notice) return;
      const id = window.setTimeout(() => setNotice(null), 5000);
      return function stop() {
        window.clearTimeout(id);
      };
    },
    [notice],
  );

  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(200);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [depth, setDepth] = useState("");
  const [ignoreParams, setIgnoreParams] = useState(false);
  const [renderJs, setRenderJs] = useState(false);
  const activeSettings =
    (include.trim() ? 1 : 0) + (exclude.trim() ? 1 : 0) + (depth ? 1 : 0) + (ignoreParams ? 1 : 0) + (renderJs ? 1 : 0);

  useEffect(function readMine() {
    setMine(loadMine());
    const stored = loadPins();
    pinsRef.current = stored;
    setPins(stored);
  }, []);

  function updatePins(next: number[]) {
    pinsRef.current = next;
    savePins(next);
    setPins(next);
  }

  const select = useCallback(
    function select(id: number | null) {
      router.replace(id === null ? pathname : pathname + "?job=" + id, { scroll: false });
    },
    [router, pathname],
  );

  const refreshList = useCallback(async function refreshList() {
    try {
      const data = await listPublicCrawls(pinsRef.current);
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

  // Open the top crawl straight away rather than showing an empty panel.
  useEffect(
    function openLatest() {
      if (selectedId === null && ordered.length > 0) select(ordered[0].id);
    },
    [selectedId, ordered, select],
  );

  function rememberToken(id: number, token: string) {
    const next = { ...loadMine(), [String(id)]: token };
    saveMine(next);
    setMine(next);
  }

  /** Everything the Crawls menu can do. */
  async function runAction(job: PublicJob, action: MenuAction) {
    setMenu(null);
    const token = mine[String(job.id)];
    try {
      if (action === "open") {
        select(job.id);
      } else if (action === "pin" || action === "unpin") {
        const rest = pins.filter((id) => id !== job.id);
        updatePins(action === "pin" ? [job.id, ...rest].slice(0, 50) : rest);
        setNotice(action === "pin" ? "Pinned to the top of your list." : "Unpinned.");
        if (action === "pin") void refreshList();
      } else if (action === "delete") {
        if (!token) return;
        if (
          !window.confirm(
            "Delete the crawl of " + hostOf(job.seed_url) + " and everything it found? This cannot be undone.",
          )
        ) {
          return;
        }
        await deletePublicCrawl(job.id, token);
        const nextMine = { ...loadMine() };
        delete nextMine[String(job.id)];
        saveMine(nextMine);
        setMine(nextMine);
        updatePins(pins.filter((id) => id !== job.id));
        setJobs((prev) => (prev ? prev.filter((j) => j.id !== job.id) : prev));
        if (selectedId === job.id) select(null);
        setNotice("Crawl deleted.");
        void refreshList();
      } else if (action === "pause" || action === "resume" || action === "stop") {
        if (!token) return;
        if (
          action === "stop" &&
          !window.confirm(
            "Stop crawling " + hostOf(job.seed_url) + "? Pages already crawled are kept, but the crawl cannot be resumed.",
          )
        ) {
          return;
        }
        await controlPublicCrawl(job.id, action, token);
        setNotice(
          action === "pause"
            ? job.status === "queued"
              ? "Crawl paused. It will not start until you resume it."
              : "Pausing. The crawler finishes the pages in progress, then stops."
            : action === "resume"
              ? "Crawl resumed. It continues from where it stopped."
              : "Stopping the crawl.",
        );
        void refreshList();
      } else if (action === "again") {
        const res = await startPublicCrawl(
          job.seed_url,
          job.whole_site ? "all" : job.max_pages || 200,
          job.settings ? { ...job.settings } : undefined,
        );
        if (res.cancel_token) rememberToken(res.job.id, res.cancel_token);
        select(res.job.id);
        setNotice(
          res.existing ? "That site is already being crawled, so this is that crawl." : "Crawl started again with the same settings.",
        );
        void refreshList();
      } else if (action === "visit") {
        window.open(job.seed_url, "_blank", "noopener,noreferrer");
      } else if (action === "copy") {
        await navigator.clipboard.writeText(job.seed_url);
        setNotice("Address copied.");
      } else if (action === "export") {
        window.location.href = publicExportUrl(job.id);
      }
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : "That did not work. Try again.");
    }
  }

  async function onStart(event: FormEvent) {
    event.preventDefault();
    if (starting || url.trim() === "") return;
    setStarting(true);
    setStartError(null);
    try {
      const res = await startPublicCrawl(url.trim(), maxPages === WHOLE_SITE ? "all" : maxPages, {
        include: splitRules(include),
        exclude: splitRules(exclude),
        depth: depth ? Number(depth) : null,
        ignore_params: ignoreParams,
        render: renderJs,
      });
      if (res.cancel_token) rememberToken(res.job.id, res.cancel_token);
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
      <TopBar overview={overview}>
        <form className="cr-form" onSubmit={onStart} aria-label="Crawl a website">
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
            <span className="cr-sr">How many pages</span>
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
          <button
            type="button"
            className={settingsOpen ? "cr-btn cr-set-btn is-open" : "cr-btn cr-set-btn"}
            aria-expanded={settingsOpen}
            aria-controls="cr-settings"
            onClick={function toggle() {
              setSettingsOpen((v) => !v);
            }}
          >
            <SlidersHorizontal size={15} aria-hidden="true" />
            Settings
            {activeSettings > 0 ? <span className="cr-set-n">{activeSettings}</span> : null}
          </button>
          <button className="cr-btn cr-btn-go" type="submit" disabled={starting || url.trim() === ""}>
            {starting ? <Loader2 size={15} className="cr-spin" aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
            {starting ? "Starting…" : "Start crawl"}
          </button>
        </form>
      </TopBar>
      <h1 className="cr-sr">Site crawler</h1>
      {settingsOpen ? (
        <section id="cr-settings" className="cr-settings" aria-label="Crawl settings">
          <label className="cr-set-field">
            <span>Only crawl URLs containing</span>
            <textarea
              rows={2}
              placeholder={"/blog/\n/products/*/reviews"}
              value={include}
              onChange={(e) => setInclude(e.target.value)}
            />
          </label>
          <label className="cr-set-field">
            <span>Skip URLs containing</span>
            <textarea
              rows={2}
              placeholder={"?sort=\n/tag/"}
              value={exclude}
              onChange={(e) => setExclude(e.target.value)}
            />
          </label>
          <div className="cr-set-col">
            <label className="cr-set-field">
              <span>Maximum depth</span>
              <select value={depth} onChange={(e) => setDepth(e.target.value)}>
                <option value="">Any depth</option>
                {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
                  <option key={n} value={n}>
                    {n} {n === 1 ? "click" : "clicks"} from start
                  </option>
                ))}
              </select>
            </label>
            <label className="cr-set-check">
              <input type="checkbox" checked={ignoreParams} onChange={(e) => setIgnoreParams(e.target.checked)} />
              <span>Ignore query strings (treat ?page=2 as the same URL)</span>
            </label>
            <label className="cr-set-check">
              <input type="checkbox" checked={renderJs} onChange={(e) => setRenderJs(e.target.checked)} />
              <span>Render JavaScript (first 500 pages, slower)</span>
            </label>
          </div>
          <p className="cr-set-note">
            One rule per line or comma. Rules match any part of the URL, and <code>*</code> matches anything. The
            start page is always crawled.
          </p>
        </section>
      ) : null}
      {startError ? (
        <p className="cr-error cr-strip" role="alert">
          {startError}
        </p>
      ) : null}
      {notice ? (
        <p className="cr-warn cr-strip" role="status">
          {notice}
        </p>
      ) : null}
      {overview && overview.workers_online === 0 ? (
        <p className="cr-warn cr-strip" role="status">
          No crawler machine is online right now. Crawls you start will wait in the queue until one connects.
        </p>
      ) : null}

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
            {ordered.map(function row(job) {
              const active = isActive(job.status);
              return (
                <li key={job.id} className="cr-job-item">
                  <button
                    type="button"
                    className={job.id === selectedId ? "cr-job is-selected" : "cr-job"}
                    aria-current={job.id === selectedId ? "true" : undefined}
                    onClick={function pick() {
                      select(job.id);
                    }}
                    onContextMenu={function openMenu(e) {
                      e.preventDefault();
                      menuReturn.current = e.currentTarget;
                      setMenu({ job: job, x: e.clientX, y: e.clientY });
                    }}
                  >
                    <span className={"cr-dot is-" + job.status} aria-hidden="true" />
                    <span className="cr-job-main">
                      <span className="cr-job-host">
                        {pins.includes(job.id) ? (
                          <Pin size={12} className="cr-pin" aria-label="Pinned" />
                        ) : null}
                        <span className="cr-job-name">{hostOf(job.seed_url)}</span>
                        {mine[String(job.id)] ? <span className="cr-mine">yours</span> : null}
                      </span>
                      <span className="cr-job-meta">
                        {STATUS_WORD[job.status] || job.status}
                        {job.pause_requested && active ? " · pausing" : ""}
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
                  <button
                    type="button"
                    className="cr-job-more"
                    aria-label={"Actions for " + hostOf(job.seed_url)}
                    aria-haspopup="menu"
                    aria-expanded={menu !== null && menu.job.id === job.id}
                    onClick={function openMenu(e) {
                      const rect = e.currentTarget.getBoundingClientRect();
                      menuReturn.current = e.currentTarget;
                      setMenu({ job: job, x: rect.right - 220, y: rect.bottom + 4 });
                    }}
                  >
                    <MoreVertical size={15} aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
          {overview ? (
            <p className="cr-fine">
              Crawls stay on the site you enter: no subdomains, no other sites. Respects robots.txt,{" "}
              {overview.limits.requests_per_second} requests a second, whole site up to{" "}
              {num(overview.limits.whole_site_max)} pages.
            </p>
          ) : null}
        </aside>

        <main className="cr-detail">
          {selectedId === null ? (
            <div className="cr-empty">
              <strong>{jobs !== null && jobs.length === 0 ? "No crawls yet" : "Opening the latest crawl…"}</strong>
              <p>Paste a website address in the bar above and press Start crawl.</p>
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

      {menu ? (
        <CrawlMenu
          job={menu.job}
          x={menu.x}
          y={menu.y}
          canControl={Boolean(mine[String(menu.job.id)])}
          pinned={pins.includes(menu.job.id)}
          onAction={function act(action) {
            void runAction(menu.job, action);
          }}
          onClose={closeMenu}
        />
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------- top bar */

function TopBar({ overview, children }: { overview: Overview | null; children?: React.ReactNode }) {
  const online = overview ? overview.workers_online : null;
  return (
    <header className="cr-top">
      <Link href="/crawl" className="cr-brand">
        <LogoMark size={22} />
        <span>Honeycomb</span>
        <span className="cr-brand-sub">Site crawler</span>
      </Link>
      {children}
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
  const paused = job ? job.status === "paused" : false;

  // A pause, resume or stop made from the Crawls menu reaches this view through
  // the list. Taking it means a paused view starts polling again on resume.
  useEffect(
    function takeListChanges() {
      if (!fromList) return;
      setJob(function merge(prev) {
        if (
          prev &&
          (prev.status !== fromList.status ||
            prev.pause_requested !== fromList.pause_requested ||
            prev.cancel_requested !== fromList.cancel_requested)
        ) {
          return { ...prev, ...fromList };
        }
        return prev;
      });
    },
    [fromList],
  );

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

  async function control(action: CrawlAction) {
    if (!cancelToken || stopping) return;
    if (action === "stop" && !window.confirm("Stop this crawl? Pages already crawled are kept, but it cannot be resumed.")) {
      return;
    }
    setStopping(true);
    try {
      const data = await controlPublicCrawl(jobId, action, cancelToken);
      setJob(data.job);
      onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "That did not work. Try again.");
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
          <h2 className="cr-head-host" title={job.seed_url}>
            {hostOf(job.seed_url)}
          </h2>
          <p className="cr-head-meta" aria-live="polite">
            <span className={"cr-dot is-" + job.status} aria-hidden="true" />
            {STATUS_WORD[job.status] || job.status}
            {job.cancel_requested && active ? " · stopping" : ""}
            {job.pause_requested && active && !job.cancel_requested ? " · pausing" : ""}
            {job.status === "queued" && typeof job.queue_position === "number"
              ? " · " + (job.queue_position === 0 ? "starts next" : job.queue_position + " crawls ahead")
              : ""}
            {job.settings?.render ? " · JS rendered" : ""}
            {job.settings && (job.settings.include.length || job.settings.exclude.length) ? " · URL rules" : ""}
          </p>
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

        <div className="cr-head-actions">
          <div className="cr-seg" role="tablist" aria-label="Crawl views">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "pages"}
              className={tab === "pages" ? "is-on" : undefined}
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
              className={tab === "console" ? "is-on" : undefined}
              onClick={function show() {
                setTab("console");
              }}
            >
              Live log
            </button>
          </div>
          {cancelToken && active && !job.cancel_requested && !job.pause_requested ? (
            <button
              type="button"
              className="cr-btn cr-btn-ghost"
              onClick={() => void control("pause")}
              disabled={stopping}
            >
              <Pause size={13} aria-hidden="true" />
              Pause
            </button>
          ) : null}
          {cancelToken && (paused || (active && job.pause_requested)) && !job.cancel_requested ? (
            <button
              type="button"
              className="cr-btn cr-btn-ghost"
              onClick={() => void control("resume")}
              disabled={stopping}
            >
              <Play size={13} aria-hidden="true" />
              {paused ? "Resume" : "Keep crawling"}
            </button>
          ) : null}
          {cancelToken && (active || paused) && !job.cancel_requested ? (
            <button
              type="button"
              className="cr-btn cr-btn-ghost"
              onClick={() => void control("stop")}
              disabled={stopping}
            >
              <Square size={13} aria-hidden="true" />
              Stop
            </button>
          ) : null}
          <a className="cr-btn cr-btn-ghost" href={publicExportUrl(job.id)} download>
            <Download size={14} aria-hidden="true" />
            Export all
          </a>
        </div>
      </div>
      {active ? (
        <div className="cr-bar cr-bar-lg" aria-hidden="true">
          <span style={{ width: progress(job) + "%" }} />
        </div>
      ) : null}

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
