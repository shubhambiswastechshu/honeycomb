"use client";

/**
 * Search & keywords: where the money goes on the search side, and how good the
 * ads are at earning it.
 *
 * Search terms are the real queries people typed. The report splits them into
 * the ones that spent without ever converting (candidates for negative
 * keywords) and the ones that paid for themselves. Keywords and Quality Score
 * say why some are cheap and some are not.
 */

import { useMemo, useState } from "react";
import {
  derive,
  fmtCompact,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtRatio,
  humanize,
  noteOf,
  parseKeywords,
  parseQuality,
  parseSearchTerms,
} from "@/components/reports/google-ads/ads-model";
import type { KeywordRow, QualityRow, TermRow } from "@/components/reports/google-ads/ads-model";
import { Card, DataTable, Empty, Pill, Segmented, Slot } from "@/components/reports/google-ads/ads-ui";
import type { Column } from "@/components/reports/google-ads/ads-ui";
import type { ReportCtx } from "@/components/reports/google-ads/ads-packs";

type ScoreFilter = "ALL" | "LOW" | "GOOD";

function ScoreChip({ score }: { score: number | null }) {
  if (score === null) {
    return <span className="ga-muted">{"—"}</span>;
  }
  const tone = score <= 4 ? "bad" : score <= 7 ? "warn" : "good";
  return (
    <span className={"ga-score is-" + tone} title={"Quality Score " + String(score) + " out of 10"}>
      {score}
    </span>
  );
}

function ComponentChip({ value }: { value: string }) {
  if (value === "ABOVE_AVERAGE") {
    return <Pill tone="good">Above avg</Pill>;
  }
  if (value === "BELOW_AVERAGE") {
    return <Pill tone="bad">Below avg</Pill>;
  }
  if (value === "AVERAGE") {
    return <Pill tone="muted">Average</Pill>;
  }
  return <span className="ga-muted">{"—"}</span>;
}

function Stat({ label, value, tone, sub }: { label: string; value: string; tone?: "bad" | "good"; sub?: string }) {
  return (
    <li className={tone !== undefined ? "ga-stat is-" + tone : "ga-stat"}>
      <span className="ga-stat-value">{value}</span>
      <span className="ga-stat-label">{label}</span>
      {sub !== undefined ? <span className="ga-stat-sub">{sub}</span> : null}
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* Search terms                                                        */
/* ------------------------------------------------------------------ */

function termColumns(kind: "waste" | "win", currency: string): Array<Column<TermRow>> {
  const money = function money(n: number): string {
    return fmtMoney(n, currency, { compact: true });
  };
  const term: Column<TermRow> = {
    id: "term",
    label: "Search term",
    sort: function s(t) {
      return t.term;
    },
    cell: function c(t) {
      return (
        <span className="ga-cell-stack is-left">
          <span className="ga-strong" title={t.term}>{t.term}</span>
          <span className="ga-muted ga-tiny" title={t.campaign}>{humanize(t.matchType) + " · " + t.campaign}</span>
        </span>
      );
    },
    csv: function v(t) {
      return t.term;
    },
    className: "ga-col-name",
  };
  if (kind === "waste") {
    return [
      term,
      {
        id: "clicks",
        label: "Clicks",
        align: "right",
        sort: function s(t) {
          return t.clicks;
        },
        cell: function c(t) {
          return fmtCompact(t.clicks);
        },
        csv: function v(t) {
          return t.clicks;
        },
      },
      {
        id: "impressions",
        label: "Impr.",
        align: "right",
        sort: function s(t) {
          return t.impressions;
        },
        cell: function c(t) {
          return fmtCompact(t.impressions);
        },
        csv: function v(t) {
          return t.impressions;
        },
      },
      {
        id: "cost",
        label: "Wasted",
        align: "right",
        sort: function s(t) {
          return t.cost;
        },
        cell: function c(t) {
          return <span className="ga-bad">{money(t.cost)}</span>;
        },
        csv: function v(t) {
          return t.cost;
        },
      },
    ];
  }
  return [
    term,
    {
      id: "conversions",
      label: "Conv.",
      align: "right",
      sort: function s(t) {
        return t.conversions;
      },
      cell: function c(t) {
        return fmtNum(t.conversions, 1);
      },
      csv: function v(t) {
        return t.conversions;
      },
    },
    {
      id: "cpa",
      label: "Cost / conv.",
      align: "right",
      sort: function s(t) {
        return t.costPerConv;
      },
      cell: function c(t) {
        return t.costPerConv === null ? "—" : money(t.costPerConv);
      },
      csv: function v(t) {
        return t.costPerConv;
      },
    },
    {
      id: "roas",
      label: "ROAS",
      align: "right",
      sort: function s(t) {
        return t.roas;
      },
      cell: function c(t) {
        return t.roas === null ? "—" : <span className="ga-good">{fmtRatio(t.roas)}</span>;
      },
      csv: function v(t) {
        return t.roas;
      },
    },
  ];
}

/* ------------------------------------------------------------------ */
/* Keywords                                                            */
/* ------------------------------------------------------------------ */

function KeywordTable({ rows, currency }: { rows: KeywordRow[]; currency: string }) {
  const [filter, setFilter] = useState<ScoreFilter>("ALL");
  const shown = useMemo(
    function byScore() {
      if (filter === "LOW") {
        return rows.filter(function low(k) {
          return k.qualityScore !== null && k.qualityScore <= 4;
        });
      }
      if (filter === "GOOD") {
        return rows.filter(function good(k) {
          return k.qualityScore !== null && k.qualityScore >= 8;
        });
      }
      return rows;
    },
    [rows, filter]
  );
  const money = function money(n: number): string {
    return fmtMoney(n, currency, { compact: true });
  };

  return (
    <DataTable
      rows={shown}
      caption="Keyword performance"
      rowKey={function key(k) {
        return k.id;
      }}
      initialSort={{ id: "cost", dir: "desc" }}
      exportName="google-ads-keywords"
      pageSize={15}
      empty="No keyword had any activity in this period."
      search={{
        placeholder: "Search keywords or campaigns",
        text: function text(k) {
          return k.text + " " + k.campaign + " " + k.adGroup;
        },
      }}
      filters={
        <Segmented<ScoreFilter>
          label="Quality Score"
          small
          value={filter}
          onChange={setFilter}
          options={[
            { id: "ALL", label: "All" },
            { id: "LOW", label: "Score 1–4" },
            { id: "GOOD", label: "Score 8–10" },
          ]}
        />
      }
      columns={[
        {
          id: "text",
          label: "Keyword",
          sort: function s(k) {
            return k.text;
          },
          cell: function c(k) {
            return (
              <span className="ga-cell-stack is-left">
                <span className="ga-strong" title={k.text}>{k.text}</span>
                <span className="ga-muted ga-tiny" title={k.campaign + " › " + k.adGroup}>
                  {humanize(k.matchType) + " · " + k.campaign}
                </span>
              </span>
            );
          },
          csv: function v(k) {
            return k.text;
          },
          className: "ga-col-name",
        },
        {
          id: "qs",
          label: "QS",
          align: "right",
          hint: "Quality Score, 1 to 10.",
          sort: function s(k) {
            return k.qualityScore;
          },
          cell: function c(k) {
            return <ScoreChip score={k.qualityScore} />;
          },
          csv: function v(k) {
            return k.qualityScore;
          },
        },
        {
          id: "cost",
          label: "Spend",
          align: "right",
          sort: function s(k) {
            return k.cost;
          },
          cell: function c(k) {
            return money(k.cost);
          },
          csv: function v(k) {
            return k.cost;
          },
        },
        {
          id: "clicks",
          label: "Clicks",
          align: "right",
          sort: function s(k) {
            return k.clicks;
          },
          cell: function c(k) {
            return fmtCompact(k.clicks);
          },
          csv: function v(k) {
            return k.clicks;
          },
        },
        {
          id: "ctr",
          label: "CTR",
          align: "right",
          sort: function s(k) {
            return derive(k).ctr;
          },
          cell: function c(k) {
            return k.impressions > 0 ? fmtPct(derive(k).ctr) : "—";
          },
          csv: function v(k) {
            return Math.round(derive(k).ctr * 100) / 100;
          },
        },
        {
          id: "cpc",
          label: "Avg. CPC",
          align: "right",
          sort: function s(k) {
            return derive(k).avgCpc;
          },
          cell: function c(k) {
            return k.clicks > 0 ? fmtMoney(derive(k).avgCpc, currency) : "—";
          },
          csv: function v(k) {
            return Math.round(derive(k).avgCpc * 100) / 100;
          },
        },
        {
          id: "conversions",
          label: "Conv.",
          align: "right",
          sort: function s(k) {
            return k.conversions;
          },
          cell: function c(k) {
            return fmtNum(k.conversions, 1);
          },
          csv: function v(k) {
            return k.conversions;
          },
        },
        {
          id: "cpa",
          label: "Cost / conv.",
          align: "right",
          sort: function s(k) {
            return derive(k).costPerConv;
          },
          cell: function c(k) {
            const cpa = derive(k).costPerConv;
            return cpa === null ? "—" : money(cpa);
          },
          csv: function v(k) {
            const cpa = derive(k).costPerConv;
            return cpa === null ? null : Math.round(cpa * 100) / 100;
          },
        },
      ]}
    />
  );
}

/* ------------------------------------------------------------------ */
/* Panel                                                               */
/* ------------------------------------------------------------------ */

export default function SearchPanel({ ctx }: { ctx: ReportCtx }) {
  const money = function money(n: number): string {
    return fmtMoney(n, ctx.currency, { compact: true });
  };

  return (
    <div className="ga-stack">
      <Card
        title="Search terms"
        note="The real queries behind your clicks: which ones burned budget without a conversion, and which paid for themselves."
        wide
      >
        <Slot slice={ctx.slice("terms")} onRetry={ctx.retry} height={300}>
          {function render(payload) {
            const terms = parseSearchTerms(payload);
            if (terms === null || terms.examined === 0) {
              return <Empty>{noteOf(payload) || "No search terms in this period."}</Empty>;
            }
            return (
              <>
                <ul className="ga-stats">
                  <Stat label="Search terms examined" value={fmtCompact(terms.examined)} />
                  <Stat label="Spend on those terms" value={money(terms.totalCost)} />
                  <Stat
                    label="Spent with no conversion"
                    value={money(terms.wastedCost)}
                    tone={terms.wastedCost > 0 ? "bad" : "good"}
                  />
                  <Stat
                    label="Share of spend wasted"
                    value={fmtPct(terms.wastePercent, 1)}
                    tone={terms.wastePercent >= 20 ? "bad" : undefined}
                  />
                </ul>
                <div className="ga-grid-2 is-tight">
                  <div>
                    <h4 className="ga-subhead">Wasted spend</h4>
                    <p className="ga-subnote">
                      Terms that cost money and never converted. Adding the clearly irrelevant ones as negative keywords in Google Ads stops the spend.
                    </p>
                    <DataTable
                      rows={terms.wasters}
                      caption="Search terms with spend and no conversions"
                      rowKey={function key(t) {
                        return t.term + "|" + t.campaign + "|" + t.matchType;
                      }}
                      initialSort={{ id: "cost", dir: "desc" }}
                      search={{
                        placeholder: "Search these terms",
                        text: function text(t) {
                          return t.term + " " + t.campaign;
                        },
                      }}
                      exportName="google-ads-wasted-search-terms"
                      pageSize={10}
                      empty="No search term wasted meaningful spend."
                      columns={termColumns("waste", ctx.currency)}
                    />
                  </div>
                  <div>
                    <h4 className="ga-subhead">Best performers</h4>
                    <p className="ga-subnote">
                      Terms that converted, ranked by return on ad spend. Candidates to add as exact-match keywords or to raise bids on.
                    </p>
                    <DataTable
                      rows={terms.winners}
                      caption="Search terms with conversions"
                      rowKey={function key(t) {
                        return t.term + "|" + t.campaign + "|" + t.matchType;
                      }}
                      initialSort={{ id: "roas", dir: "desc" }}
                      search={{
                        placeholder: "Search these terms",
                        text: function text(t) {
                          return t.term + " " + t.campaign;
                        },
                      }}
                      exportName="google-ads-winning-search-terms"
                      pageSize={10}
                      empty="No search term converted in this period."
                      columns={termColumns("win", ctx.currency)}
                    />
                  </div>
                </div>
                <p className="ga-foot-note">
                  {"Looked at the " + fmtCompact(terms.examined) + " most expensive terms; each list shows the top 50."}
                </p>
              </>
            );
          }}
        </Slot>
      </Card>

      <Card title="Keywords" note="Every keyword that ran in this period, with its Quality Score." wide>
        <Slot slice={ctx.slice("keywords")} onRetry={ctx.retry} height={300}>
          {function render(payload) {
            const rows = parseKeywords(payload);
            if (rows.length === 0) {
              return <Empty>{noteOf(payload) || "No keyword had any activity in this period."}</Empty>;
            }
            return <KeywordTable rows={rows} currency={ctx.currency} />;
          }}
        </Slot>
      </Card>

      <Card
        title="Quality Score"
        note="How Google rates your keywords and ads. Low scores pay more per click for less reach."
        wide
      >
        <Slot slice={ctx.slice("quality")} onRetry={ctx.retry} height={240}>
          {function render(payload) {
            const q = parseQuality(payload);
            if (q === null || q.rows.length === 0) {
              return <Empty>{noteOf(payload) || "No enabled keywords to score."}</Empty>;
            }
            const total = q.great + q.average + q.poor + q.unscored;
            const seg = function seg(n: number): string {
              return String(total > 0 ? (n / total) * 100 : 0) + "%";
            };
            const low = q.rows
              .filter(function poor(r) {
                return r.score !== null && r.score <= 4;
              })
              .sort(function byScore(a, b) {
                return (a.score || 0) - (b.score || 0);
              });
            const columns: Array<Column<QualityRow>> = [
              {
                id: "keyword",
                label: "Keyword",
                sort: function s(r) {
                  return r.keyword;
                },
                cell: function c(r) {
                  return (
                    <span className="ga-cell-stack is-left">
                      <span className="ga-strong" title={r.keyword}>{r.keyword}</span>
                      <span className="ga-muted ga-tiny" title={r.campaign + " › " + r.adGroup}>
                        {humanize(r.matchType) + " · " + r.campaign}
                      </span>
                    </span>
                  );
                },
                csv: function v(r) {
                  return r.keyword;
                },
                className: "ga-col-name",
              },
              {
                id: "score",
                label: "QS",
                align: "right",
                sort: function s(r) {
                  return r.score;
                },
                cell: function c(r) {
                  return <ScoreChip score={r.score} />;
                },
                csv: function v(r) {
                  return r.score;
                },
              },
              {
                id: "ctr",
                label: "Expected CTR",
                sort: function s(r) {
                  return r.expectedCtr;
                },
                cell: function c(r) {
                  return <ComponentChip value={r.expectedCtr} />;
                },
                csv: function v(r) {
                  return r.expectedCtr;
                },
              },
              {
                id: "relevance",
                label: "Ad relevance",
                sort: function s(r) {
                  return r.adRelevance;
                },
                cell: function c(r) {
                  return <ComponentChip value={r.adRelevance} />;
                },
                csv: function v(r) {
                  return r.adRelevance;
                },
              },
              {
                id: "landing",
                label: "Landing page",
                sort: function s(r) {
                  return r.landingPage;
                },
                cell: function c(r) {
                  return <ComponentChip value={r.landingPage} />;
                },
                csv: function v(r) {
                  return r.landingPage;
                },
              },
            ];
            return (
              <>
                <div className="ga-dist" role="img" aria-label={
                  String(q.great) + " keywords score 8 to 10, " + String(q.average) + " score 5 to 7, " +
                  String(q.poor) + " score 1 to 4, " + String(q.unscored) + " have no score"
                }>
                  <span className="ga-dist-great" style={{ width: seg(q.great) }} />
                  <span className="ga-dist-avg" style={{ width: seg(q.average) }} />
                  <span className="ga-dist-poor" style={{ width: seg(q.poor) }} />
                  <span className="ga-dist-none" style={{ width: seg(q.unscored) }} />
                </div>
                <ul className="ga-dist-legend">
                  <li><span className="ga-swatch is-won" /> <b>{q.great}</b> great (8–10)</li>
                  <li><span className="ga-swatch is-amber" /> <b>{q.average}</b> average (5–7)</li>
                  <li><span className="ga-swatch is-rank" /> <b>{q.poor}</b> poor (1–4)</li>
                  <li><span className="ga-swatch is-none" /> <b>{q.unscored}</b> not scored yet</li>
                </ul>
                <h4 className="ga-subhead">Keywords to fix first</h4>
                <p className="ga-subnote">
                  Score 1–4, worst first. The three columns on the right are the reasons Google gives: fix the ones marked below average.
                </p>
                <DataTable
                  rows={low}
                  columns={columns}
                  caption="Keywords with a poor Quality Score"
                  rowKey={function key(r) {
                    return r.id + "|" + r.adGroup;
                  }}
                  initialSort={{ id: "score", dir: "asc" }}
                  search={{
                    placeholder: "Search these keywords",
                    text: function text(r) {
                      return r.keyword + " " + r.campaign + " " + r.adGroup;
                    },
                  }}
                  exportName="google-ads-low-quality-keywords"
                  pageSize={12}
                  empty="No keyword has a Quality Score of 4 or below. Nothing to fix here."
                />
              </>
            );
          }}
        </Slot>
      </Card>
    </div>
  );
}
