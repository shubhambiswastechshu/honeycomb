"use client";

/**
 * The right-hand sidebar: Screaming Frog's Issues, Overview, Site Structure and
 * Response Times panels, plus reports, the XML sitemap and crawl comparison.
 *
 * Every count is clickable and filters the grid, because a number you cannot
 * drill into is a number nobody acts on.
 */

import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { compareCrawls, listReports, runReport, sitemapUrl } from "@/lib/crawlWorkspace";
import type { CompareResult, ReportResult, ReportSummary, Severity, Workspace } from "@/lib/crawlWorkspace";
import type { PublicJob } from "@/lib/crawl";
import SiteTree from "@/components/crawl/SiteTree";
import SitemapAudit from "@/components/crawl/SitemapAudit";
import { codeClass, num, pathOf } from "@/components/crawl/format";

const SEVERITIES: { key: Severity; label: string }[] = [
  { key: "high", label: "High priority" },
  { key: "medium", label: "Medium priority" },
  { key: "low", label: "Low priority" },
];

function Bars({ rows }: { rows: { label: string; value: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="ins-bars">
      {rows.map(function bar(r) {
        return (
          <li key={r.label}>
            <span className="ins-bar-label">{r.label}</span>
            <span className="ins-bar-track" aria-hidden="true">
              <span style={{ width: (100 * r.value) / max + "%" }} />
            </span>
            <span className="ins-bar-n">{num(r.value)}</span>
          </li>
        );
      })}
    </ul>
  );
}

export default function Insights({
  jobId,
  data,
  activeTab,
  activeFilter,
  activeIssue,
  onPick,
  onIssue,
  onFolder,
  compareWith,
}: {
  jobId: number;
  data: Workspace;
  activeTab: string;
  activeFilter: string;
  activeIssue: string;
  onPick: (tab: string, filter: string) => void;
  onIssue: (code: string) => void;
  onFolder: (path: string) => void;
  compareWith: PublicJob[];
}) {
  const [treeOpen, setTreeOpen] = useState(false);
  const [sitemapOpen, setSitemapOpen] = useState(false);
  const [reports, setReports] = useState<ReportSummary[] | null>(null);
  const [report, setReport] = useState<ReportResult | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [against, setAgainst] = useState<number | "">("");
  const [comparison, setComparison] = useState<CompareResult | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(
    function loadReports() {
      let cancelled = false;
      listReports(jobId)
        .then((r) => {
          if (!cancelled) setReports(r.reports);
        })
        .catch(() => {
          if (!cancelled) setReports([]);
        });
      return function stop() {
        cancelled = true;
      };
    },
    [jobId],
  );

  async function openReport(name: string) {
    setReportError(null);
    setReport(null);
    try {
      setReport(await runReport(jobId, name));
    } catch (caught) {
      setReportError(caught instanceof Error ? caught.message : "Could not run that report.");
    }
  }

  async function compare() {
    if (against === "") return;
    setCompareError(null);
    setComparison(null);
    try {
      setComparison(await compareCrawls(jobId, against));
    } catch (caught) {
      setCompareError(caught instanceof Error ? caught.message : "Could not compare those crawls.");
    }
  }

  const issuesBy = (severity: Severity) => data.issues.filter((i) => i.severity === severity);

  return (
    <aside className="ins" aria-label="Crawl insights">
      <details className="ins-sec" open>
        <summary>
          Issues
          <span className="ins-sum">
            <span className="ws-sev is-high">{data.issue_totals.high}</span>
            <span className="ws-sev is-medium">{data.issue_totals.medium}</span>
            <span className="ws-sev is-low">{data.issue_totals.low}</span>
          </span>
        </summary>
        {data.issues.length === 0 ? <p className="ins-empty">No issues found yet.</p> : null}
        {SEVERITIES.map(function group(s) {
          const rows = issuesBy(s.key);
          if (rows.length === 0) return null;
          return (
            <div key={s.key} className="ins-group">
              <p className="ins-group-title">{s.label}</p>
              <ul className="ins-list">
                {rows.map(function issue(i) {
                  return (
                    <li key={i.code}>
                      <button
                        type="button"
                        className={activeIssue === i.code ? "ins-row is-on" : "ins-row"}
                        title={i.why || undefined}
                        onClick={function onClick() {
                          onIssue(i.code);
                        }}
                      >
                        <span className={"ins-dot is-" + i.severity} aria-hidden="true" />
                        <span className="ins-row-label">{i.label}</span>
                        <span className="ins-row-n">{num(i.pages)}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </details>

      <details className="ins-sec">
        <summary>Overview</summary>
        {data.tabs.map(function tabGroup(t) {
          return (
            <div key={t.key} className="ins-group">
              <p className="ins-group-title">{t.label}</p>
              <ul className="ins-list">
                {t.filters.map(function f(filter) {
                  const on = !activeIssue && activeTab === t.key && activeFilter === filter.key;
                  return (
                    <li key={filter.key}>
                      <button
                        type="button"
                        className={on ? "ins-row is-on" : "ins-row"}
                        onClick={function onClick() {
                          onPick(t.key, filter.key);
                        }}
                      >
                        <span className="ins-row-label">{filter.label}</span>
                        <span className="ins-row-n">{num(filter.count)}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </details>

      <details
        className="ins-sec"
        onToggle={function toggled(e) {
          if ((e.currentTarget as HTMLDetailsElement).open) setTreeOpen(true);
        }}
      >
        <summary>Site structure</summary>
        {treeOpen ? <SiteTree jobId={jobId} onFolder={onFolder} /> : null}
        <p className="ins-group-title ins-pad">Crawl depth</p>
        <p className="ins-note">URLs by clicks from the start page.</p>
        <Bars rows={data.structure.map((s) => ({ label: "Depth " + s.depth, value: s.pages }))} />
      </details>

      <details className="ins-sec">
        <summary>Response times</summary>
        <Bars rows={data.response_times.map((r) => ({ label: r.bucket, value: r.pages }))} />
      </details>

      <details className="ins-sec">
        <summary>Status codes &amp; indexability</summary>
        <ul className="ins-list ins-static">
          {data.status_codes.map(function code(c) {
            return (
              <li key={String(c.code)} className="ins-row">
                <span className={codeClass(c.code)}>{c.code ?? "No response"}</span>
                <span className="ins-row-n">{num(c.pages)}</span>
              </li>
            );
          })}
        </ul>
        <ul className="ins-list ins-static">
          {data.indexability.map(function idx(r) {
            return (
              <li key={r.reason} className="ins-row">
                <span className={r.indexable ? "cr-idx is-yes" : "cr-idx"}>{r.reason}</span>
                <span className="ins-row-n">{num(r.pages)}</span>
              </li>
            );
          })}
        </ul>
      </details>

      <details
        className="ins-sec"
        onToggle={function toggled(e) {
          if ((e.currentTarget as HTMLDetailsElement).open) setSitemapOpen(true);
        }}
      >
        <summary>Sitemap audit</summary>
        {sitemapOpen ? <SitemapAudit jobId={jobId} /> : null}
      </details>

      <details className="ins-sec">
        <summary>Reports</summary>
        {reports === null ? <p className="ins-empty">Loading…</p> : null}
        <ul className="ins-list">
          {(reports || []).map(function r(rep) {
            return (
              <li key={rep.name}>
                <button
                  type="button"
                  className={report?.name === rep.name ? "ins-row is-on" : "ins-row"}
                  title={rep.description}
                  onClick={function onClick() {
                    void openReport(rep.name);
                  }}
                >
                  <span className="ins-row-label">{rep.title}</span>
                </button>
              </li>
            );
          })}
        </ul>
        {reportError ? <p className="cr-error">{reportError}</p> : null}
        {report ? (
          <div className="ins-report">
            <p className="ins-group-title">
              {report.title} · {num(report.count)} {report.truncated ? "(first 200)" : ""}
            </p>
            <p className="ins-note">{report.description}</p>
            {report.rows.length === 0 ? (
              <p className="ins-empty">Nothing to report.</p>
            ) : (
              <div className="ins-report-wrap">
                <table className="ins-report-table">
                  <thead>
                    <tr>
                      {report.columns.map((c) => (
                        <th key={c}>{c.replace(/_/g, " ")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {report.rows.map(function row(r, i) {
                      return (
                        <tr key={i}>
                          {r.map((v, j) => {
                            const s = Array.isArray(v) ? v.join(", ") : v === null || v === undefined ? "—" : String(v);
                            return (
                              <td key={j} title={s}>
                                {s.startsWith("http") ? pathOf(s) : s}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : null}
      </details>

      <details className="ins-sec">
        <summary>Tools</summary>
        <a className="cr-btn cr-btn-ghost ins-tool" href={sitemapUrl(jobId)} download>
          <Download size={14} aria-hidden="true" />
          XML sitemap (indexable 200s)
        </a>
        <div className="ins-compare">
          <p className="ins-group-title">Compare with an earlier crawl</p>
          {compareWith.length === 0 ? (
            <p className="ins-note">Crawl this site again later to compare the two.</p>
          ) : (
            <>
              <select
                value={against === "" ? "" : String(against)}
                onChange={function onChange(e) {
                  setAgainst(e.target.value ? Number(e.target.value) : "");
                }}
              >
                <option value="">Choose a crawl…</option>
                {compareWith.map((j) => (
                  <option key={j.id} value={j.id}>
                    #{j.id} · {new Date(j.created_at).toLocaleString("en-GB")} · {num(j.pages_crawled)} pages
                  </option>
                ))}
              </select>
              <button type="button" className="cr-btn cr-btn-ghost" onClick={compare} disabled={against === ""}>
                Compare
              </button>
            </>
          )}
          {compareError ? <p className="cr-error">{compareError}</p> : null}
          {comparison ? (
            <div className="ins-report">
              <ul className="ins-list ins-static">
                <li className="ins-row">
                  <span>New URLs</span>
                  <span className="ins-row-n">{num(comparison.summary.added)}</span>
                </li>
                <li className="ins-row">
                  <span>Removed URLs</span>
                  <span className="ins-row-n">{num(comparison.summary.removed)}</span>
                </li>
                <li className="ins-row">
                  <span>Status changed</span>
                  <span className="ins-row-n">{num(comparison.summary.status_changed)}</span>
                </li>
                <li className="ins-row">
                  <span>Title changed</span>
                  <span className="ins-row-n">{num(comparison.summary.title_changed)}</span>
                </li>
              </ul>
              {comparison.status_changed.slice(0, 20).map((c) => (
                <p key={c.url} className="ins-note" title={c.url}>
                  {pathOf(c.url)}: {c.was ?? "—"} → {c.now ?? "—"}
                </p>
              ))}
            </div>
          ) : null}
        </div>
      </details>
    </aside>
  );
}
