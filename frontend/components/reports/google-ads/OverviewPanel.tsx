"use client";

/**
 * Overview: how the account did, against the period before it.
 *
 * Reads top to bottom the way a person asks: what happened (highlights, in
 * sentences), how much (ten headline numbers, each against the previous
 * period), when (the daily trend, with spikes called out), and where it went
 * (campaigns, devices and networks).
 */

import { useMemo, useState } from "react";
import { Lightbulb } from "lucide-react";
import {
  KPIS,
  buildHighlights,
  dayLong,
  deltaOf,
  fillDays,
  fmtCompact,
  fmtKpi,
  fmtMoney,
  fmtNum,
  fmtPct,
  humanize,
  deviceName,
  kpiSeries,
  kpiValue,
  networkName,
  noteOf,
  parseCampaigns,
  parseDaily,
  parseSegment,
  sumMetrics,
  totalsOf,
} from "@/components/reports/google-ads/ads-model";
import type { CampaignRow, DayRow, SegmentRow, Totals } from "@/components/reports/google-ads/ads-model";
import { TREND_METRICS, TrendChart } from "@/components/reports/google-ads/AdsCharts";
import type { TrendMetric } from "@/components/reports/google-ads/AdsCharts";
import {
  Card,
  DataTable,
  DeltaChip,
  Empty,
  Segmented,
  ShareBar,
  Slot,
  Sparkline,
} from "@/components/reports/google-ads/ads-ui";
import type { Column } from "@/components/reports/google-ads/ads-ui";
import type { ReportCtx } from "@/components/reports/google-ads/ads-packs";
import type { Slice } from "@/components/reports/google-ads/useReportPack";

/* ------------------------------------------------------------------ */
/* KPI grid                                                            */
/* ------------------------------------------------------------------ */

function KpiGrid({
  days,
  current,
  previous,
  previousState,
  currency,
}: {
  days: DayRow[];
  current: Totals;
  previous: Totals | null;
  /** Why there is no comparison, when there is not one, so the page can say so. */
  previousState: "off" | "loading" | "failed" | "ready";
  currency: string;
}) {
  return (
    <div>
      <ul className="ga-kpis">
        {KPIS.map(function tile(def) {
          const value = kpiValue(current, def.key);
          const before = previous === null ? null : kpiValue(previous, def.key);
          const delta = previous === null ? null : deltaOf(value, before);
          return (
            <li className="ga-kpi" key={def.key} title={def.hint}>
              <span className="ga-kpi-label">{def.label}</span>
              <span className="ga-kpi-value">{fmtKpi(def.kind, value, currency)}</span>
              <span className="ga-kpi-foot">
                <DeltaChip delta={delta} good={def.good} />
                <Sparkline values={kpiSeries(days, def.key)} />
              </span>
              {previous !== null && before !== null ? (
                <span className="ga-kpi-prev">{"was " + fmtKpi(def.kind, before, currency)}</span>
              ) : null}
            </li>
          );
        })}
      </ul>
      {previousState === "failed" ? (
        <p className="ga-foot-note">The previous period could not be loaded, so there is nothing to compare against.</p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Highlights                                                          */
/* ------------------------------------------------------------------ */

function Highlights({
  current,
  previous,
  campaigns,
  days,
  currency,
}: {
  current: Totals;
  previous: Totals | null;
  campaigns: CampaignRow[];
  days: DayRow[];
  currency: string;
}) {
  const items = useMemo(
    function build() {
      return buildHighlights({ current, previous, campaigns, days, currency });
    },
    [current, previous, campaigns, days, currency]
  );
  if (items.length === 0) {
    return null;
  }
  return (
    <Card title="Highlights" note="Written from the numbers on this page, nothing else.">
      <ul className="ga-highlights">
        {items.map(function each(item, i) {
          return (
            <li key={String(i)} className={"ga-highlight is-" + item.tone}>
              <Lightbulb size={14} strokeWidth={2} aria-hidden="true" />
              <span>{item.text}</span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Segment tables                                                      */
/* ------------------------------------------------------------------ */

function segmentColumns(
  labelOf: (key: string) => string,
  heading: string,
  currency: string,
  totalCost: number
): Array<Column<SegmentRow>> {
  return [
    {
      id: "key",
      label: heading,
      sort: function s(r) {
        return labelOf(r.key);
      },
      cell: function c(r) {
        return <span className="ga-strong">{labelOf(r.key)}</span>;
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
        return (
          <span className="ga-cell-stack">
            <span>{fmtMoney(r.cost, currency, { compact: true })}</span>
            <ShareBar value={r.cost} max={totalCost} tone="blue" />
          </span>
        );
      },
      csv: function v(r) {
        return r.cost;
      },
    },
    {
      id: "share",
      label: "Share",
      align: "right",
      sort: function s(r) {
        return totalCost > 0 ? r.cost / totalCost : 0;
      },
      cell: function c(r) {
        return totalCost > 0 ? fmtPct((r.cost / totalCost) * 100, 1) : "—";
      },
    },
    {
      id: "clicks",
      label: "Clicks",
      align: "right",
      sort: function s(r) {
        return r.clicks;
      },
      cell: function c(r) {
        return fmtCompact(r.clicks);
      },
      csv: function v(r) {
        return r.clicks;
      },
    },
    {
      id: "ctr",
      label: "CTR",
      align: "right",
      sort: function s(r) {
        return r.impressions > 0 ? r.clicks / r.impressions : 0;
      },
      cell: function c(r) {
        return r.impressions > 0 ? fmtPct((r.clicks / r.impressions) * 100) : "—";
      },
    },
    {
      id: "conversions",
      label: "Conv.",
      align: "right",
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
      id: "cpa",
      label: "Cost / conv.",
      align: "right",
      sort: function s(r) {
        return r.conversions > 0 ? r.cost / r.conversions : null;
      },
      cell: function c(r) {
        return r.conversions > 0 ? fmtMoney(r.cost / r.conversions, currency, { compact: true }) : "—";
      },
    },
  ];
}

function SegmentCard({
  title,
  note,
  heading,
  field,
  labelOf,
  currency,
  slice,
  retry,
}: {
  title: string;
  note: string;
  heading: string;
  field: string;
  labelOf: (key: string) => string;
  currency: string;
  slice: Slice;
  retry: () => void;
}) {
  return (
    <Card title={title} note={note}>
      <Slot slice={slice} onRetry={retry} height={150}>
        {function render(payload) {
          const rows = parseSegment(payload, field).sort(function bySpend(a, b) {
            return b.cost - a.cost;
          });
          const total = sumMetrics(rows).cost;
          if (rows.length === 0) {
            return <Empty>{noteOf(payload) || "No data for this period."}</Empty>;
          }
          return (
            <DataTable
              rows={rows}
              columns={segmentColumns(labelOf, heading, currency, total)}
              rowKey={function key(r) {
                return r.key;
              }}
              caption={title}
              initialSort={{ id: "cost", dir: "desc" }}
              pageSize={12}
            />
          );
        }}
      </Slot>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Panel                                                               */
/* ------------------------------------------------------------------ */

export default function OverviewPanel({ ctx }: { ctx: ReportCtx }) {
  const [metric, setMetric] = useState<TrendMetric>("cost");
  const [showDays, setShowDays] = useState<boolean>(false);

  const daily = ctx.slice("daily");
  const prev = ctx.compare ? ctx.slice("daily_prev") : null;
  const campaignsSlice = ctx.slice("campaigns");

  const days = useMemo(
    function currentDays() {
      return fillDays(parseDaily(daily.data), ctx.window);
    },
    [daily.data, ctx.window]
  );
  const prevDays = useMemo(
    function previousDays() {
      return prev !== null && prev.status === "ready"
        ? fillDays(parseDaily(prev.data), ctx.prevWindow)
        : null;
    },
    [prev, ctx.prevWindow]
  );
  const campaigns = useMemo(
    function rows() {
      return campaignsSlice.status === "ready" ? parseCampaigns(campaignsSlice.data) : [];
    },
    [campaignsSlice]
  );

  const current = useMemo(
    function totals() {
      return totalsOf(days);
    },
    [days]
  );
  const previous = useMemo(
    function prevTotals() {
      return prevDays === null ? null : totalsOf(prevDays);
    },
    [prevDays]
  );

  const previousState: "off" | "loading" | "failed" | "ready" =
    prev === null ? "off" : prev.status === "ready" ? "ready" : prev.status === "error" ? "failed" : "loading";

  const metricDef = TREND_METRICS.filter(function is(m) {
    return m.id === metric;
  })[0];
  const dailyEmpty = daily.status === "ready" && current.impressions === 0 && current.cost === 0;
  const topCampaigns = campaigns
    .filter(function spent(c) {
      return c.cost > 0;
    })
    .sort(function bySpend(a, b) {
      return b.cost - a.cost;
    })
    .slice(0, 6);
  const campaignSpend = campaigns.reduce(function add(sum, c) {
    return sum + c.cost;
  }, 0);

  return (
    <div className="ga-stack">
      {daily.status === "ready" && !dailyEmpty ? (
        <Highlights current={current} previous={previous} campaigns={campaigns} days={days} currency={ctx.currency} />
      ) : null}

      <Card
        title="Performance"
        note={
          ctx.compare
            ? "Each figure against the same number of days immediately before."
            : "Comparison with the previous period is switched off."
        }
      >
        <Slot slice={daily} onRetry={ctx.retry} height={210}>
          {function render(payload) {
            if (dailyEmpty) {
              return <Empty>{noteOf(payload) || "This account had no activity in the selected period."}</Empty>;
            }
            return (
              <KpiGrid
                days={days}
                current={current}
                previous={previous}
                previousState={previousState}
                currency={ctx.currency}
              />
            );
          }}
        </Slot>
      </Card>

      <Card
        title="Daily trend"
        note="Bars are days. The solid line is the seven-day average; the dashed line is the previous period."
        actions={
          <Segmented<TrendMetric>
            label="Metric"
            small
            value={metric}
            onChange={setMetric}
            options={TREND_METRICS.map(function o(m) {
              return { id: m.id, label: m.label };
            })}
          />
        }
        wide
      >
        <Slot slice={daily} onRetry={ctx.retry} height={270}>
          {function render() {
            if (dailyEmpty) {
              return <Empty>No daily data to draw.</Empty>;
            }
            return (
              <>
                <TrendChart days={days} previous={prevDays} currency={ctx.currency} metric={metric} />
                <div className="ga-legend" aria-hidden="true">
                  <span className="ga-key">
                    <span className="ga-swatch" style={{ background: metricDef.color }} /> {metricDef.label}
                  </span>
                  <span className="ga-key">
                    <span className="ga-line is-solid" style={{ borderColor: metricDef.color }} /> 7-day average
                  </span>
                  {prevDays !== null ? (
                    <span className="ga-key">
                      <span className="ga-line is-dashed" /> Previous period
                    </span>
                  ) : null}
                  <span className="ga-key">
                    <span className="ga-tri" /> Spike
                  </span>
                </div>
                <details
                  className="ga-details"
                  onToggle={function toggled(event) {
                    setShowDays((event.currentTarget as HTMLDetailsElement).open);
                  }}
                >
                  <summary>Show the numbers</summary>
                  {showDays ? (
                    <DataTable
                      rows={days.slice().reverse()}
                      caption="Daily totals, newest first"
                      rowKey={function key(d) {
                        return d.date;
                      }}
                      pageSize={31}
                      exportName="google-ads-daily"
                      columns={[
                        {
                          id: "date",
                          label: "Day",
                          sort: function s(d) {
                            return d.date;
                          },
                          cell: function c(d) {
                            return dayLong(d.date);
                          },
                          csv: function v(d) {
                            return d.date;
                          },
                        },
                        {
                          id: "cost",
                          label: "Spend",
                          align: "right",
                          sort: function s(d) {
                            return d.cost;
                          },
                          cell: function c(d) {
                            return fmtMoney(d.cost, ctx.currency);
                          },
                          csv: function v(d) {
                            return d.cost;
                          },
                        },
                        {
                          id: "impressions",
                          label: "Impressions",
                          align: "right",
                          sort: function s(d) {
                            return d.impressions;
                          },
                          cell: function c(d) {
                            return fmtCompact(d.impressions);
                          },
                          csv: function v(d) {
                            return d.impressions;
                          },
                        },
                        {
                          id: "clicks",
                          label: "Clicks",
                          align: "right",
                          sort: function s(d) {
                            return d.clicks;
                          },
                          cell: function c(d) {
                            return fmtCompact(d.clicks);
                          },
                          csv: function v(d) {
                            return d.clicks;
                          },
                        },
                        {
                          id: "conversions",
                          label: "Conv.",
                          align: "right",
                          sort: function s(d) {
                            return d.conversions;
                          },
                          cell: function c(d) {
                            return fmtNum(d.conversions, 1);
                          },
                          csv: function v(d) {
                            return d.conversions;
                          },
                        },
                        {
                          id: "value",
                          label: "Conv. value",
                          align: "right",
                          sort: function s(d) {
                            return d.conversionValue;
                          },
                          cell: function c(d) {
                            return fmtMoney(d.conversionValue, ctx.currency);
                          },
                          csv: function v(d) {
                            return d.conversionValue;
                          },
                        },
                      ]}
                    />
                  ) : null}
                </details>
              </>
            );
          }}
        </Slot>
      </Card>

      <div className="ga-grid-2">
        <Card
          title="Top campaigns"
          note="By spend in this period."
          actions={
            <button type="button" className="ga-link" onClick={function go() {
              ctx.goTo("campaigns");
            }}>
              All campaigns
            </button>
          }
        >
          <Slot slice={campaignsSlice} onRetry={ctx.retry} height={200}>
            {function render(payload) {
              if (topCampaigns.length === 0) {
                return <Empty>{noteOf(payload) || "No campaign spent anything in this period."}</Empty>;
              }
              return (
                <ol className="ga-rank">
                  {topCampaigns.map(function each(c) {
                    return (
                      <li key={c.id} className="ga-rank-row">
                        <span className="ga-rank-name" title={c.name}>
                          <span className={"ga-dot is-" + (c.status === "ENABLED" ? "on" : "off")} title={humanize(c.status)} />
                          {c.name}
                        </span>
                        <span className="ga-rank-num">{fmtMoney(c.cost, ctx.currency, { compact: true })}</span>
                        <ShareBar value={c.cost} max={topCampaigns[0].cost} tone="blue" />
                        <span className="ga-rank-sub">
                          {campaignSpend > 0 ? fmtPct((c.cost / campaignSpend) * 100, 1) + " of spend" : ""}
                          {" · " + fmtNum(c.conversions, 1) + " conv."}
                        </span>
                      </li>
                    );
                  })}
                </ol>
              );
            }}
          </Slot>
        </Card>

        <SegmentCard
          title="Devices"
          note="Where people saw and clicked your ads."
          heading="Device"
          field="device"
          labelOf={deviceName}
          currency={ctx.currency}
          slice={ctx.slice("devices")}
          retry={ctx.retry}
        />
      </div>

      <SegmentCard
        title="Networks"
        note="Google Search, its partners, Display and YouTube."
        heading="Network"
        field="adNetworkType"
        labelOf={networkName}
        currency={ctx.currency}
        slice={ctx.slice("networks")}
        retry={ctx.retry}
      />
    </div>
  );
}
