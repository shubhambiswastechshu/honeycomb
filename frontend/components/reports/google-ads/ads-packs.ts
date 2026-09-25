/**
 * Which tools each sub-tab of the report runs, and with what arguments.
 *
 * One place, so "what does the Campaigns tab cost in API calls" has a single
 * answer, and so a section and the request that feeds it cannot drift apart:
 * the ids here are the names the panels read their data by.
 *
 * Every run is sent EXPLICIT dates (see windowFor), never a preset, so the
 * current period and the one before it come from the same arithmetic.
 */

import type { DateWindow } from "@/components/reports/google-ads/ads-model";
import type { PackRun, Slice } from "@/components/reports/google-ads/useReportPack";

export type PackId = "overview" | "campaigns" | "search" | "timing" | "health";

export const PACKS: Array<{ id: PackId; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "campaigns", label: "Campaigns" },
  { id: "search", label: "Search & keywords" },
  { id: "timing", label: "Timing & places" },
  { id: "health", label: "Health & changes" },
];

/** What every section is handed: the account, the windows, and how to read its data. */
export interface ReportCtx {
  currency: string;
  /** The account's time zone: Google Ads cuts its days and its hours there. */
  timeZone: string;
  window: DateWindow;
  prevWindow: DateWindow;
  compare: boolean;
  /** The slice of the current pack a section reads, by run id. */
  slice: (id: string) => Slice;
  /** Re-run the whole pack, past the client cache. */
  retry: () => void;
  goTo: (pack: PackId) => void;
}

export interface PackArgs {
  customerId: string;
  loginCustomerId: string | null;
  window: DateWindow;
  prevWindow: DateWindow;
  compare: boolean;
}

/** The arguments every tool takes, plus the manager account to log in through when there is one. */
function base(a: PackArgs): Record<string, unknown> {
  const args: Record<string, unknown> = { customer_id: a.customerId };
  if (a.loginCustomerId !== null) {
    args.login_customer_id = a.loginCustomerId;
  }
  return args;
}

function dated(a: PackArgs, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...base(a),
    start_date: a.window.start,
    end_date: a.window.end,
    ...extra,
  };
}

export function buildRuns(pack: PackId, a: PackArgs): PackRun[] {
  switch (pack) {
    case "overview": {
      const runs: PackRun[] = [
        { id: "daily", tool: "get_daily_performance", args: dated(a) },
        { id: "campaigns", tool: "get_campaign_performance", args: dated(a, { limit: 500 }) },
        { id: "devices", tool: "get_device_performance", args: dated(a) },
        { id: "networks", tool: "get_network_performance", args: dated(a) },
      ];
      if (a.compare) {
        runs.push({
          id: "daily_prev",
          tool: "get_daily_performance",
          args: {
            ...base(a),
            start_date: a.prevWindow.start,
            end_date: a.prevWindow.end,
          },
        });
      }
      return runs;
    }
    case "campaigns":
      return [
        { id: "campaigns", tool: "get_campaign_performance", args: dated(a, { limit: 500 }) },
        { id: "impression_share", tool: "get_impression_share", args: dated(a, { limit: 500 }) },
        // Month-to-date by definition: the tool ignores the window.
        { id: "pacing", tool: "get_budget_pacing", args: base(a) },
        { id: "conversions", tool: "get_conversion_data", args: dated(a, { limit: 100 }) },
      ];
    case "search":
      return [
        { id: "terms", tool: "search_query_analysis", args: dated(a, { limit: 2000 }) },
        { id: "keywords", tool: "get_keyword_performance", args: dated(a, { limit: 500 }) },
        { id: "quality", tool: "get_quality_score_breakdown", args: { ...base(a), limit: 1000 } },
      ];
    case "timing":
      return [
        { id: "hours", tool: "get_hourly_performance", args: dated(a) },
        { id: "weekdays", tool: "get_day_of_week_performance", args: dated(a) },
        { id: "geo", tool: "get_geo_performance", args: dated(a, { limit: 100 }) },
      ];
    default:
      return [
        { id: "health", tool: "account_health_check", args: base(a) },
        { id: "recs", tool: "get_recommendation_impact", args: base(a) },
        { id: "changes", tool: "get_change_history", args: { ...base(a), days: 14, limit: 300 } },
      ];
  }
}

/**
 * What makes two views the same view. Health and the account-wide tools do not
 * depend on the window, so changing the range must not refetch them.
 */
export function packKey(pack: PackId, a: PackArgs): string {
  const windowed = pack === "overview" || pack === "campaigns" || pack === "search" || pack === "timing";
  return [
    pack,
    a.customerId,
    a.loginCustomerId === null ? "" : a.loginCustomerId,
    windowed ? a.window.start + ".." + a.window.end : "",
    pack === "overview" && a.compare ? "cmp" : "",
  ].join("|");
}
