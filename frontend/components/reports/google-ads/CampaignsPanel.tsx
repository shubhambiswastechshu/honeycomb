"use client";

/**
 * Campaigns: every campaign side by side, how much of the market each one is
 * winning, whether its budget is on pace, and which conversion actions are
 * doing the counting.
 *
 * The campaign table is the workhorse -- sort by any column, search by name,
 * filter by status, export what is shown. Impression share answers "where am I
 * leaving reach on the table, and is it money or ad rank that is holding me
 * back"; pacing answers "will I hit or blow this month's budget".
 */

import { useMemo, useState } from "react";
import {
  dayLong,
  fmtCompact,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtRatio,
  humanize,
  noteOf,
  parseCampaigns,
  parseConversionActions,
  parseImpressionShare,
  parsePacing,
  sumMetrics,
  derive,
} from "@/components/reports/google-ads/ads-model";
import type { CampaignRow, ImpressionShareRow, PacingRow } from "@/components/reports/google-ads/ads-model";
import { Card, DataTable, Empty, Pill, Segmented, ShareBar, Slot } from "@/components/reports/google-ads/ads-ui";
import type { Column } from "@/components/reports/google-ads/ads-ui";
import type { ReportCtx } from "@/components/reports/google-ads/ads-packs";

type StatusFilter = "ALL" | "ENABLED" | "PAUSED";

function StatusDot({ status }: { status: string }) {
  return (
    <span
      className={"ga-dot is-" + (status === "ENABLED" ? "on" : status === "PAUSED" ? "paused" : "off")}
      title={humanize(status)}
    />
  );
}

/* ------------------------------------------------------------------ */
/* Campaign table                                                      */
/* ------------------------------------------------------------------ */

function CampaignTable({ rows, currency }: { rows: CampaignRow[]; currency: string }) {
  const [status, setStatus] = useState<StatusFilter>("ALL");
  const filtered = useMemo(
    function byStatus() {
      return status === "ALL"
        ? rows
        : rows.filter(function keep(c) {
            return c.status === status;
          });
    },
    [rows, status]
  );
  const totalCost = useMemo(
    function sum() {
      return sumMetrics(filtered).cost;
    },
    [filtered]
  );
  const money = function money(n: number): string {
    return fmtMoney(n, currency, { compact: true });
  };

  const columns: Array<Column<CampaignRow>> = [
    {
      id: "name",
      label: "Campaign",
      sort: function s(c) {
        return c.name;
      },
      cell: function c(row) {
        return (
          <span className="ga-name" title={row.name}>
            <StatusDot status={row.status} />
            <span>{row.name}</span>
          </span>
        );
      },
      csv: function v(c) {
        return c.name;
      },
      className: "ga-col-name",
    },
    {
      id: "status",
      label: "Status",
      sort: function s(c) {
        return c.status;
      },
      cell: function c(row) {
        return <span className="ga-muted">{humanize(row.status)}</span>;
      },
      csv: function v(c) {
        return c.status;
      },
    },
    {
      id: "cost",
      label: "Spend",
      align: "right",
      sort: function s(c) {
        return c.cost;
      },
      cell: function c(row) {
        return (
          <span className="ga-cell-stack">
            <span>{money(row.cost)}</span>
            <ShareBar value={row.cost} max={totalCost} tone="blue" />
          </span>
        );
      },
      csv: function v(c) {
        return c.cost;
      },
    },
    {
      id: "impressions",
      label: "Impr.",
      align: "right",
      sort: function s(c) {
        return c.impressions;
      },
      cell: function c(row) {
        return fmtCompact(row.impressions);
      },
      csv: function v(c) {
        return c.impressions;
      },
    },
    {
      id: "clicks",
      label: "Clicks",
      align: "right",
      sort: function s(c) {
        return c.clicks;
      },
      cell: function c(row) {
        return fmtCompact(row.clicks);
      },
      csv: function v(c) {
        return c.clicks;
      },
    },
    {
      id: "ctr",
      label: "CTR",
      align: "right",
      sort: function s(c) {
        return derive(c).ctr;
      },
      cell: function c(row) {
        return row.impressions > 0 ? fmtPct(derive(row).ctr) : "—";
      },
      csv: function v(c) {
        return Math.round(derive(c).ctr * 100) / 100;
      },
    },
    {
      id: "cpc",
      label: "Avg. CPC",
      align: "right",
      sort: function s(c) {
        return derive(c).avgCpc;
      },
      cell: function c(row) {
        return row.clicks > 0 ? fmtMoney(derive(row).avgCpc, currency) : "—";
      },
      csv: function v(c) {
        return Math.round(derive(c).avgCpc * 100) / 100;
      },
    },
    {
      id: "conversions",
      label: "Conv.",
      align: "right",
      sort: function s(c) {
        return c.conversions;
      },
      cell: function c(row) {
        return fmtNum(row.conversions, 1);
      },
      csv: function v(c) {
        return c.conversions;
      },
    },
    {
      id: "cpa",
      label: "Cost / conv.",
      align: "right",
      sort: function s(c) {
        return derive(c).costPerConv;
      },
      cell: function c(row) {
        const cpa = derive(row).costPerConv;
        return cpa === null ? "—" : money(cpa);
      },
      csv: function v(c) {
        const cpa = derive(c).costPerConv;
        return cpa === null ? null : Math.round(cpa * 100) / 100;
      },
    },
    {
      id: "roas",
      label: "ROAS",
      align: "right",
      sort: function s(c) {
        return derive(c).roas;
      },
      cell: function c(row) {
        const roas = derive(row).roas;
        return roas === null ? "—" : fmtRatio(roas);
      },
      csv: function v(c) {
        const roas = derive(c).roas;
        return roas === null ? null : Math.round(roas * 100) / 100;
      },
    },
  ];

  return (
    <DataTable
      rows={filtered}
      columns={columns}
      rowKey={function key(c) {
        return c.id;
      }}
      caption="Campaign performance"
      initialSort={{ id: "cost", dir: "desc" }}
      search={{
        placeholder: "Search campaigns",
        text: function text(c) {
          return c.name;
        },
      }}
      filters={
        <Segmented<StatusFilter>
          label="Status"
          small
          value={status}
          onChange={setStatus}
          options={[
            { id: "ALL", label: "All" },
            { id: "ENABLED", label: "Enabled" },
            { id: "PAUSED", label: "Paused" },
          ]}
        />
      }
      exportName="google-ads-campaigns"
      pageSize={15}
      empty="No campaign had any activity in this period."
      footer={function totals(shown) {
        const t = derive(sumMetrics(shown));
        return (
          <tr>
            <th scope="row" colSpan={2}>
              {"Total · " + String(shown.length) + (shown.length === 1 ? " campaign" : " campaigns")}
            </th>
            <td className="is-num">{money(t.cost)}</td>
            <td className="is-num">{fmtCompact(t.impressions)}</td>
            <td className="is-num">{fmtCompact(t.clicks)}</td>
            <td className="is-num">{t.impressions > 0 ? fmtPct(t.ctr) : "—"}</td>
            <td className="is-num">{t.clicks > 0 ? fmtMoney(t.avgCpc, currency) : "—"}</td>
            <td className="is-num">{fmtNum(t.conversions, 1)}</td>
            <td className="is-num">{t.costPerConv === null ? "—" : money(t.costPerConv)}</td>
            <td className="is-num">{t.roas === null ? "—" : fmtRatio(t.roas)}</td>
          </tr>
        );
      }}
    />
  );
}

/* ------------------------------------------------------------------ */
/* Impression share                                                    */
/* ------------------------------------------------------------------ */

function ShareStack({ row }: { row: ImpressionShareRow }) {
  // Won + lost to budget + lost to rank is the whole auction; anything left
  // over is share Google does not attribute either way.
  const won = Math.max(0, Math.min(100, row.share));
  const budget = Math.max(0, Math.min(100 - won, row.lostBudget));
  const rank = Math.max(0, Math.min(100 - won - budget, row.lostRank));
  return (
    <span className="ga-stack-bar" role="img" aria-label={
      fmtPct(won, 1) + " won, " + fmtPct(budget, 1) + " lost to budget, " + fmtPct(rank, 1) + " lost to rank"
    }>
      <span className="ga-stack-won" style={{ width: String(won) + "%" }} />
      <span className="ga-stack-budget" style={{ width: String(budget) + "%" }} />
      <span className="ga-stack-rank" style={{ width: String(rank) + "%" }} />
    </span>
  );
}

function ImpressionShareTable({ rows, currency }: { rows: ImpressionShareRow[]; currency: string }) {
  const budgetLimited = rows
    .filter(function limited(r) {
      return r.lostBudget >= 15;
    })
    .sort(function byLoss(a, b) {
      return b.lostBudget - a.lostBudget;
    });
  const rankLimited = rows
    .filter(function limited(r) {
      return r.lostRank >= 25;
    })
    .sort(function byLoss(a, b) {
      return b.lostRank - a.lostRank;
    });

  const columns: Array<Column<ImpressionShareRow>> = [
    {
      id: "name",
      label: "Campaign",
      sort: function s(r) {
        return r.name;
      },
      cell: function c(r) {
        return <span className="ga-strong" title={r.name}>{r.name}</span>;
      },
      csv: function v(r) {
        return r.name;
      },
      className: "ga-col-name",
    },
    {
      id: "bar",
      label: "Where the auctions went",
      cell: function c(r) {
        return <ShareStack row={r} />;
      },
      className: "ga-col-bar",
    },
    {
      id: "share",
      label: "Won",
      align: "right",
      sort: function s(r) {
        return r.share;
      },
      cell: function c(r) {
        return fmtPct(r.share, 1);
      },
      csv: function v(r) {
        return r.share;
      },
      hint: "Search impression share: impressions you got out of those you were eligible for.",
    },
    {
      id: "budget",
      label: "Lost: budget",
      align: "right",
      sort: function s(r) {
        return r.lostBudget;
      },
      cell: function c(r) {
        return <span className={r.lostBudget >= 15 ? "ga-warn" : undefined}>{fmtPct(r.lostBudget, 1)}</span>;
      },
      csv: function v(r) {
        return r.lostBudget;
      },
      hint: "Missed because the daily budget ran out.",
    },
    {
      id: "rank",
      label: "Lost: rank",
      align: "right",
      sort: function s(r) {
        return r.lostRank;
      },
      cell: function c(r) {
        return <span className={r.lostRank >= 25 ? "ga-bad" : undefined}>{fmtPct(r.lostRank, 1)}</span>;
      },
      csv: function v(r) {
        return r.lostRank;
      },
      hint: "Missed because the ad ranked too low: bid or quality.",
    },
    {
      id: "top",
      label: "Top of page",
      align: "right",
      sort: function s(r) {
        return r.topShare;
      },
      cell: function c(r) {
        return fmtPct(r.topShare, 1);
      },
      csv: function v(r) {
        return r.topShare;
      },
    },
    {
      id: "cost",
      label: "Spend",
      align: "right",
      sort: function s(r) {
        return r.cost;
      },
      cell: function c(r) {
        return fmtMoney(r.cost, currency, { compact: true });
      },
      csv: function v(r) {
        return r.cost;
      },
    },
  ];

  return (
    <div>
      {budgetLimited.length > 0 || rankLimited.length > 0 ? (
        <ul className="ga-callouts">
          {budgetLimited.length > 0 ? (
            <li className="ga-callout is-warn">
              <b>{String(budgetLimited.length) + (budgetLimited.length === 1 ? " campaign is" : " campaigns are")}</b>
              {" budget-limited, missing 15% or more of the auctions because the daily budget ran out. Worst: "}
              <b>{budgetLimited[0].name}</b>
              {" (" + fmtPct(budgetLimited[0].lostBudget, 0) + " lost)."}
            </li>
          ) : null}
          {rankLimited.length > 0 ? (
            <li className="ga-callout is-bad">
              <b>{String(rankLimited.length) + (rankLimited.length === 1 ? " campaign is" : " campaigns are")}</b>
              {" losing 25% or more to ad rank, which points at bids or quality rather than budget. Worst: "}
              <b>{rankLimited[0].name}</b>
              {" (" + fmtPct(rankLimited[0].lostRank, 0) + " lost)."}
            </li>
          ) : null}
        </ul>
      ) : null}
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={function key(r) {
          return r.id;
        }}
        caption="Search impression share by campaign"
        initialSort={{ id: "cost", dir: "desc" }}
        exportName="google-ads-impression-share"
        pageSize={12}
        empty="No search campaign reported impression share in this period."
      />
      <div className="ga-legend" aria-hidden="true">
        <span className="ga-key"><span className="ga-swatch is-won" /> Won</span>
        <span className="ga-key"><span className="ga-swatch is-budget" /> Lost to budget</span>
        <span className="ga-key"><span className="ga-swatch is-rank" /> Lost to rank</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Budget pacing                                                       */
/* ------------------------------------------------------------------ */

function PaceCard({ row, currency }: { row: PacingRow; currency: string }) {
  const money = function money(n: number): string {
    return fmtMoney(n, currency, { compact: true });
  };
  const scale = Math.max(row.targetEom, row.projectedEom, row.mtdSpend, 1);
  const spentPct = (row.mtdSpend / scale) * 100;
  const expectedPct = (row.expectedMtd / scale) * 100;
  const tone: "good" | "bad" | "warn" | "muted" =
    row.pace === "overpacing" ? "bad" : row.pace === "underpacing" ? "warn" : row.pace === "on_pace" ? "good" : "muted";
  const words =
    row.pace === "overpacing" ? "Over pace" : row.pace === "underpacing" ? "Under pace" : row.pace === "on_pace" ? "On pace" : humanize(row.pace);
  return (
    <li className="ga-pace">
      <div className="ga-pace-head">
        <span className="ga-name" title={row.name}>
          <StatusDot status={row.status} />
          <span>{row.name}</span>
        </span>
        <Pill tone={tone}>{words}</Pill>
      </div>
      <div className="ga-pace-bar" role="img" aria-label={
        money(row.mtdSpend) + " spent so far, " + money(row.expectedMtd) + " expected by now at the daily budget"
      }>
        <span className="ga-pace-spent" style={{ width: String(Math.min(spentPct, 100)) + "%" }} />
        <span className="ga-pace-mark" style={{ left: String(Math.min(expectedPct, 100)) + "%" }} title="Where a steady daily budget would be by now" />
      </div>
      <dl className="ga-pace-facts">
        <div><dt>Spent</dt><dd>{money(row.mtdSpend)}</dd></div>
        <div><dt>Daily budget</dt><dd>{row.dailyBudget > 0 ? money(row.dailyBudget) : "—"}</dd></div>
        <div><dt>Projected</dt><dd>{money(row.projectedEom)}</dd></div>
        <div>
          <dt>vs budget</dt>
          <dd className={row.variance > 0 ? "ga-bad" : row.variance < 0 ? "ga-warn" : undefined}>
            {(row.variance > 0 ? "+" : row.variance < 0 ? "−" : "") + money(Math.abs(row.variance))}
          </dd>
        </div>
      </dl>
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* Panel                                                               */
/* ------------------------------------------------------------------ */

export default function CampaignsPanel({ ctx }: { ctx: ReportCtx }) {
  const [pacingLimit, setPacingLimit] = useState<number>(8);

  return (
    <div className="ga-stack">
      <Card title="Campaign performance" note="Every campaign in the selected period. Click a column to sort." wide>
        <Slot slice={ctx.slice("campaigns")} onRetry={ctx.retry} height={300}>
          {function render(payload) {
            const rows = parseCampaigns(payload);
            if (rows.length === 0) {
              return <Empty>{noteOf(payload) || "No campaign had any activity in this period."}</Empty>;
            }
            return <CampaignTable rows={rows} currency={ctx.currency} />;
          }}
        </Slot>
      </Card>

      <Card
        title="Impression share"
        note="Of the searches your ads could have shown for, how many they did -- and what stopped the rest."
        wide
      >
        <Slot slice={ctx.slice("impression_share")} onRetry={ctx.retry} height={240}>
          {function render(payload) {
            const rows = parseImpressionShare(payload);
            if (rows.length === 0) {
              return <Empty>{noteOf(payload) || "No search campaign reported impression share in this period."}</Empty>;
            }
            return <ImpressionShareTable rows={rows} currency={ctx.currency} />;
          }}
        </Slot>
      </Card>

      <Card
        title="Budget pacing"
        note="Month to date, whatever range is selected above."
        wide
      >
        <Slot slice={ctx.slice("pacing")} onRetry={ctx.retry} height={200}>
          {function render(payload) {
            const pacing = parsePacing(payload);
            if (pacing === null || pacing.rows.length === 0) {
              return <Empty>{noteOf(payload) || "No campaign has a daily budget or spend this month."}</Empty>;
            }
            const rows = pacing.rows.slice().sort(function bySpend(a, b) {
              return b.mtdSpend - a.mtdSpend;
            });
            const over = rows.filter(function is(r) {
              return r.pace === "overpacing";
            }).length;
            const under = rows.filter(function is(r) {
              return r.pace === "underpacing";
            }).length;
            return (
              <>
                <p className="ga-foot-note is-top">
                  {"As of " + (pacing.asOf ? dayLong(pacing.asOf) : "today") + " (day " + String(pacing.daysElapsed) + " of the month). "}
                  {String(over) + " over pace, " + String(under) + " under pace, " + String(rows.length - over - under) + " on pace."}
                </p>
                <ul className="ga-paces">
                  {rows.slice(0, pacingLimit).map(function card(row) {
                    return <PaceCard key={row.id} row={row} currency={ctx.currency} />;
                  })}
                </ul>
                {rows.length > pacingLimit ? (
                  <div className="ga-table-more">
                    <span>{"Showing " + String(pacingLimit) + " of " + String(rows.length)}</span>
                    <button type="button" className="ga-btn is-quiet" onClick={function more() {
                      setPacingLimit(pacingLimit + 8);
                    }}>
                      Show more
                    </button>
                  </div>
                ) : null}
              </>
            );
          }}
        </Slot>
      </Card>

      <Card title="Conversion actions" note="What is being counted as a conversion, and what each is worth." wide>
        <Slot slice={ctx.slice("conversions")} onRetry={ctx.retry} height={200}>
          {function render(payload) {
            const rows = parseConversionActions(payload);
            if (rows.length === 0) {
              return <Empty>{noteOf(payload) || "No conversion actions recorded any conversions in this period."}</Empty>;
            }
            return (
              <DataTable
                rows={rows}
                caption="Conversions by action"
                rowKey={function key(r) {
                  return r.name;
                }}
                initialSort={{ id: "all", dir: "desc" }}
                exportName="google-ads-conversion-actions"
                pageSize={12}
                columns={[
                  {
                    id: "name",
                    label: "Action",
                    sort: function s(r) {
                      return r.name;
                    },
                    cell: function c(r) {
                      return <span className="ga-strong">{r.name}</span>;
                    },
                    csv: function v(r) {
                      return r.name;
                    },
                    className: "ga-col-name",
                  },
                  {
                    id: "category",
                    label: "Category",
                    sort: function s(r) {
                      return r.category;
                    },
                    cell: function c(r) {
                      return <span className="ga-muted">{humanize(r.category)}</span>;
                    },
                    csv: function v(r) {
                      return r.category;
                    },
                  },
                  {
                    id: "conversions",
                    label: "Conversions",
                    align: "right",
                    hint: "Counted toward the conversions column in your campaigns.",
                    sort: function s(r) {
                      return r.conversions;
                    },
                    cell: function c(r) {
                      return fmtNum(r.conversions, 1);
                    },
                    csv: function v(r) {
                      return r.conversions;
                    },
                  },
                  {
                    id: "value",
                    label: "Value",
                    align: "right",
                    sort: function s(r) {
                      return r.value;
                    },
                    cell: function c(r) {
                      return fmtMoney(r.value, ctx.currency, { compact: true });
                    },
                    csv: function v(r) {
                      return r.value;
                    },
                  },
                  {
                    id: "all",
                    label: "All conversions",
                    align: "right",
                    hint: "Including actions not counted as primary conversions.",
                    sort: function s(r) {
                      return r.allConversions;
                    },
                    cell: function c(r) {
                      return fmtNum(r.allConversions, 1);
                    },
                    csv: function v(r) {
                      return r.allConversions;
                    },
                  },
                  {
                    id: "allValue",
                    label: "All value",
                    align: "right",
                    sort: function s(r) {
                      return r.allValue;
                    },
                    cell: function c(r) {
                      return fmtMoney(r.allValue, ctx.currency, { compact: true });
                    },
                    csv: function v(r) {
                      return r.allValue;
                    },
                  },
                ]}
              />
            );
          }}
        </Slot>
      </Card>
    </div>
  );
}
