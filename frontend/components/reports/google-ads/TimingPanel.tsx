"use client";

/**
 * Timing & places: when the account performs, and where.
 *
 * Hour of day and day of week answer the scheduling question -- when do clicks
 * turn into conversions, and when is money spent on the ones that do not. The
 * best few bars are picked out, and a sentence says how much of the total they
 * carry, so "Tuesday afternoons" is a finding rather than something to squint
 * at a chart for.
 *
 * Every hour and weekday is Google's own bucketing in the ACCOUNT's time zone;
 * the panel says so, because "14:00" means nothing until the zone is named.
 */

import { useMemo, useState } from "react";
import {
  derive,
  fmtCompact,
  fmtMoney,
  fmtNum,
  fmtPct,
  noteOf,
  parseGeo,
  parseSegment,
  sumMetrics,
} from "@/components/reports/google-ads/ads-model";
import type { Metrics, SegmentRow } from "@/components/reports/google-ads/ads-model";
import { ColumnChart } from "@/components/reports/google-ads/AdsCharts";
import type { ColumnItem } from "@/components/reports/google-ads/AdsCharts";
import { Card, DataTable, Empty, Segmented, ShareBar, Slot } from "@/components/reports/google-ads/ads-ui";
import type { ReportCtx } from "@/components/reports/google-ads/ads-packs";

type TimingMetric = "clicks" | "cost" | "conversions" | "ctr" | "convRate";

const METRICS: Array<{ id: TimingMetric; label: string; color: string; additive: boolean }> = [
  { id: "clicks", label: "Clicks", color: "#0f9d9d", additive: true },
  { id: "cost", label: "Spend", color: "#1a73e8", additive: true },
  { id: "conversions", label: "Conversions", color: "#30a14e", additive: true },
  { id: "ctr", label: "CTR", color: "#8e6cd1", additive: false },
  { id: "convRate", label: "Conv. rate", color: "#e8a23f", additive: false },
];

const WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"];
const WEEKDAY_LABEL: Record<string, string> = {
  MONDAY: "Mon",
  TUESDAY: "Tue",
  WEDNESDAY: "Wed",
  THURSDAY: "Thu",
  FRIDAY: "Fri",
  SATURDAY: "Sat",
  SUNDAY: "Sun",
};
const WEEKDAY_FULL: Record<string, string> = {
  MONDAY: "Monday",
  TUESDAY: "Tuesday",
  WEDNESDAY: "Wednesday",
  THURSDAY: "Thursday",
  FRIDAY: "Friday",
  SATURDAY: "Saturday",
  SUNDAY: "Sunday",
};

const ZERO: Metrics = { impressions: 0, clicks: 0, cost: 0, conversions: 0, conversionValue: 0 };

function valueOf(m: Metrics, metric: TimingMetric): number {
  const t = derive(m);
  return metric === "clicks"
    ? t.clicks
    : metric === "cost"
    ? t.cost
    : metric === "conversions"
    ? t.conversions
    : metric === "ctr"
    ? t.ctr
    : t.convRate;
}

function detailOf(m: Metrics, currency: string): string {
  return (
    fmtCompact(m.clicks) + " clicks · " +
    fmtMoney(m.cost, currency, { compact: true }) + " · " +
    fmtNum(m.conversions, 1) + " conv."
  );
}

function makeFormat(metric: TimingMetric, currency: string): (n: number) => string {
  return function format(n: number): string {
    if (metric === "cost") {
      return fmtMoney(n, currency, { compact: true, decimals: n >= 10 ? 0 : 2 });
    }
    if (metric === "ctr" || metric === "convRate") {
      return fmtPct(n, 1);
    }
    return metric === "conversions" ? fmtNum(n, n >= 100 ? 0 : 1) : fmtCompact(n);
  };
}

/**
 * The best few buckets and what share of the total they carry. For a ratio the
 * ranking only counts buckets with enough clicks behind them to mean something:
 * a 100% conversion rate on two clicks is not the best hour.
 */
function best(
  rows: Array<{ key: string; label: string; m: Metrics }>,
  metric: TimingMetric,
  count: number
): { labels: string[]; share: number | null } | null {
  const additive = METRICS.filter(function is(x) {
    return x.id === metric;
  })[0].additive;
  const totalClicks = rows.reduce(function add(s, r) {
    return s + r.m.clicks;
  }, 0);
  const floor = Math.max(10, totalClicks * 0.02);
  const pool = additive
    ? rows
    : rows.filter(function enough(r) {
        return r.m.clicks >= floor;
      });
  const ranked = pool
    .map(function score(r) {
      return { r: r, v: valueOf(r.m, metric) };
    })
    .filter(function positive(x) {
      return x.v > 0;
    })
    .sort(function byValue(a, b) {
      return b.v - a.v;
    })
    .slice(0, count);
  if (ranked.length === 0) {
    return null;
  }
  let share: number | null = null;
  if (additive) {
    const total = rows.reduce(function add(s, r) {
      return s + valueOf(r.m, metric);
    }, 0);
    const top = ranked.reduce(function add(s, x) {
      return s + x.v;
    }, 0);
    share = total > 0 ? (top / total) * 100 : null;
  }
  return {
    labels: ranked.map(function label(x) {
      return x.r.label;
    }),
    share: share,
  };
}

function joinLabels(labels: string[]): string {
  if (labels.length <= 1) {
    return labels.join("");
  }
  return labels.slice(0, -1).join(", ") + " and " + labels[labels.length - 1];
}

function TimingCharts({
  hours,
  weekdays,
  currency,
  timeZone,
}: {
  hours: SegmentRow[];
  weekdays: SegmentRow[];
  currency: string;
  timeZone: string;
}) {
  const [metric, setMetric] = useState<TimingMetric>("conversions");
  const def = METRICS.filter(function is(x) {
    return x.id === metric;
  })[0];
  const format = makeFormat(metric, currency);

  const hourBuckets = useMemo(
    function buildHours() {
      const by: Record<string, Metrics> = {};
      for (const row of hours) {
        by[String(Math.round(Number(row.key)))] = row;
      }
      const out: Array<{ key: string; label: string; m: Metrics }> = [];
      for (let h = 0; h < 24; h += 1) {
        out.push({ key: String(h), label: String(h).padStart(2, "0") + ":00", m: by[String(h)] || ZERO });
      }
      return out;
    },
    [hours]
  );
  const dayBuckets = useMemo(
    function buildDays() {
      const by: Record<string, Metrics> = {};
      for (const row of weekdays) {
        by[row.key] = row;
      }
      return WEEKDAYS.map(function each(d) {
        return { key: d, label: WEEKDAY_FULL[d], m: by[d] || ZERO };
      });
    },
    [weekdays]
  );

  const hourItems: ColumnItem[] = hourBuckets.map(function item(b) {
    return { key: b.key, label: b.key.padStart(2, "0"), value: valueOf(b.m, metric), detail: b.label + " · " + detailOf(b.m, currency) };
  });
  const dayItems: ColumnItem[] = dayBuckets.map(function item(b) {
    return { key: b.key, label: WEEKDAY_LABEL[b.key], value: valueOf(b.m, metric), detail: detailOf(b.m, currency) };
  });

  const total = sumMetrics(hourBuckets.map(function m(b) {
    return b.m;
  }));
  const bestHours = best(hourBuckets, metric, 3);
  const bestDays = best(dayBuckets, metric, 2);

  if (total.impressions === 0 && total.clicks === 0 && total.cost === 0) {
    return <Empty>No activity in this period, so there is no pattern to show.</Empty>;
  }

  return (
    <div className="ga-stack">
      <div className="ga-toolbar-row">
        <Segmented<TimingMetric>
          label="Metric"
          small
          value={metric}
          onChange={setMetric}
          options={METRICS.map(function o(x) {
            return { id: x.id, label: x.label };
          })}
        />
      </div>

      <ul className="ga-callouts">
        {bestHours !== null ? (
          <li className="ga-callout is-good">
            {"Best hours for " + def.label.toLowerCase() + ": "}
            <b>{joinLabels(bestHours.labels)}</b>
            {bestHours.share !== null ? " — " + fmtPct(bestHours.share, 0) + " of the total." : "."}
          </li>
        ) : null}
        {bestDays !== null ? (
          <li className="ga-callout is-good">
            {"Best days for " + def.label.toLowerCase() + ": "}
            <b>{joinLabels(bestDays.labels)}</b>
            {bestDays.share !== null ? " — " + fmtPct(bestDays.share, 0) + " of the total." : "."}
          </li>
        ) : null}
      </ul>

      <div className="ga-grid-2">
        <Card title="By hour of day" note={"Hours in the account's time zone" + (timeZone ? " (" + timeZone.replace(/_/g, " ") + ")" : "") + "."}>
          <ColumnChart
            items={hourItems}
            format={format}
            color={def.color}
            highlight={3}
            labelEvery={3}
            ariaLabel={def.label + " by hour of day"}
          />
        </Card>
        <Card title="By day of week" note="Monday first.">
          <ColumnChart
            items={dayItems}
            format={format}
            color={def.color}
            highlight={2}
            ariaLabel={def.label + " by day of week"}
          />
        </Card>
      </div>
    </div>
  );
}

export default function TimingPanel({ ctx }: { ctx: ReportCtx }) {
  const hours = ctx.slice("hours");
  const weekdays = ctx.slice("weekdays");

  // Two slices feed one view: it waits for both and shows the first failure.
  const combined =
    hours.status === "error"
      ? hours
      : weekdays.status === "error"
      ? weekdays
      : hours.status === "loading" || weekdays.status === "loading"
      ? { status: "loading" as const, data: null, error: null }
      : { status: "ready" as const, data: null, error: null, stale: hours.stale === true || weekdays.stale === true };

  return (
    <div className="ga-stack">
      <Slot slice={combined} onRetry={ctx.retry} height={340}>
        {function render() {
          return (
            <TimingCharts
              hours={parseSegment(hours.data, "hour")}
              weekdays={parseSegment(weekdays.data, "dayOfWeek")}
              currency={ctx.currency}
              timeZone={ctx.timeZone}
            />
          );
        }}
      </Slot>

      <Card title="Locations" note="Countries by spend." wide>
        <Slot slice={ctx.slice("geo")} onRetry={ctx.retry} height={220}>
          {function render(payload) {
            const geo = parseGeo(payload);
            if (geo.rows.length === 0) {
              return <Empty>{noteOf(payload) || "No location data for this period."}</Empty>;
            }
            const totalCost = geo.rows.reduce(function add(s, r) {
              return s + r.cost;
            }, 0);
            return (
              <>
                <p className="ga-foot-note is-top">{geo.basis + "."}</p>
                <DataTable
                  rows={geo.rows}
                  caption="Performance by country"
                  rowKey={function key(r) {
                    return r.id;
                  }}
                  initialSort={{ id: "cost", dir: "desc" }}
                  search={{
                    placeholder: "Search countries",
                    text: function text(r) {
                      return r.name;
                    },
                  }}
                  exportName="google-ads-locations"
                  pageSize={10}
                  columns={[
                    {
                      id: "name",
                      label: "Country",
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
                      id: "cost",
                      label: "Spend",
                      align: "right",
                      sort: function s(r) {
                        return r.cost;
                      },
                      cell: function c(r) {
                        return (
                          <span className="ga-cell-stack">
                            <span>{fmtMoney(r.cost, ctx.currency, { compact: true })}</span>
                            <ShareBar value={r.cost} max={geo.rows[0].cost} tone="blue" />
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
                        return derive(r).ctr;
                      },
                      cell: function c(r) {
                        return r.impressions > 0 ? fmtPct(derive(r).ctr) : "—";
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
                        return derive(r).costPerConv;
                      },
                      cell: function c(r) {
                        const cpa = derive(r).costPerConv;
                        return cpa === null ? "—" : fmtMoney(cpa, ctx.currency, { compact: true });
                      },
                    },
                  ]}
                />
              </>
            );
          }}
        </Slot>
      </Card>
    </div>
  );
}
