/**
 * The pure half of the Google Ads report: what the tools return, the
 * arithmetic on it, the date windows, and how numbers are written down.
 * No React and no fetching, so every rule here can be read (and tested) on its
 * own.
 *
 * Two principles run through it.
 *
 * Nothing is invented. Every figure is derived from rows a tool returned:
 * totals are sums of those rows, ratios are recomputed from the sums (a mean of
 * per-row CTRs is not the account's CTR), and a comparison with the previous
 * period exists only when that period was actually fetched. "Not enough data"
 * is a value (null), never a zero that reads as a measurement.
 *
 * Parsing is defensive. These payloads come off a provider through a
 * connector, and a field that is missing, null or a numeric string must not
 * take a whole section down with it. Every parser accepts the shape it expects
 * and degrades to an empty section for anything else.
 */

export type Json = unknown;

export function isRecord(value: Json): value is Record<string, Json> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A finite number, or 0. Accepts numeric strings: ids and counts arrive as both. */
export function num(value: Json): number {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

export function str(value: Json): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

/** The array of records under `key`, or an empty one. */
export function rowsOf(data: Json, key: string): Array<Record<string, Json>> {
  if (!isRecord(data)) {
    return [];
  }
  const list = data[key];
  return Array.isArray(list) ? list.filter(isRecord) : [];
}

/** The tool's own explanation for an empty answer -- the manager-account note, for one. */
export function noteOf(data: Json): string {
  return isRecord(data) ? str(data.note) : "";
}

/* ------------------------------------------------------------------ */
/* Metrics                                                             */
/* ------------------------------------------------------------------ */

/** The additive metrics. Everything else is derived from sums of these. */
export interface Metrics {
  impressions: number;
  clicks: number;
  cost: number;
  conversions: number;
  conversionValue: number;
}

export interface Totals extends Metrics {
  ctr: number;
  avgCpc: number;
  convRate: number;
  /** Null when there were no conversions: a cost per nothing is not zero. */
  costPerConv: number | null;
  /** Null when nothing was spent. */
  roas: number | null;
}

export function parseMetrics(row: Record<string, Json>): Metrics {
  return {
    impressions: num(row.impressions),
    clicks: num(row.clicks),
    cost: num(row.cost),
    conversions: num(row.conversions),
    conversionValue: num(row.conversion_value),
  };
}

export function derive(m: Metrics): Totals {
  return {
    impressions: m.impressions,
    clicks: m.clicks,
    cost: m.cost,
    conversions: m.conversions,
    conversionValue: m.conversionValue,
    ctr: m.impressions > 0 ? (m.clicks / m.impressions) * 100 : 0,
    avgCpc: m.clicks > 0 ? m.cost / m.clicks : 0,
    convRate: m.clicks > 0 ? (m.conversions / m.clicks) * 100 : 0,
    costPerConv: m.conversions > 0 ? m.cost / m.conversions : null,
    roas: m.cost > 0 ? m.conversionValue / m.cost : null,
  };
}

export function sumMetrics(items: Metrics[]): Metrics {
  const total: Metrics = {
    impressions: 0,
    clicks: 0,
    cost: 0,
    conversions: 0,
    conversionValue: 0,
  };
  for (const item of items) {
    total.impressions += item.impressions;
    total.clicks += item.clicks;
    total.cost += item.cost;
    total.conversions += item.conversions;
    total.conversionValue += item.conversionValue;
  }
  return total;
}

export function totalsOf(items: Metrics[]): Totals {
  return derive(sumMetrics(items));
}

export type KpiKey =
  | "cost"
  | "impressions"
  | "clicks"
  | "ctr"
  | "avgCpc"
  | "conversions"
  | "convRate"
  | "costPerConv"
  | "conversionValue"
  | "roas";

export type KpiKind = "money" | "int" | "pct" | "num" | "ratio";

export interface KpiDef {
  key: KpiKey;
  label: string;
  kind: KpiKind;
  /**
   * Which direction is an improvement. Spend is neutral on purpose: more spend
   * is neither good nor bad until it is read against what it bought.
   */
  good: "up" | "down" | "neutral";
  hint: string;
}

export const KPIS: KpiDef[] = [
  { key: "cost", label: "Spend", kind: "money", good: "neutral", hint: "Total cost in the window." },
  { key: "impressions", label: "Impressions", kind: "int", good: "up", hint: "Times your ads were shown." },
  { key: "clicks", label: "Clicks", kind: "int", good: "up", hint: "Clicks on your ads." },
  { key: "ctr", label: "CTR", kind: "pct", good: "up", hint: "Clicks divided by impressions." },
  { key: "avgCpc", label: "Avg. CPC", kind: "money", good: "down", hint: "Spend divided by clicks." },
  { key: "conversions", label: "Conversions", kind: "num", good: "up", hint: "Conversions attributed to your ads." },
  { key: "convRate", label: "Conv. rate", kind: "pct", good: "up", hint: "Conversions divided by clicks." },
  { key: "costPerConv", label: "Cost / conv.", kind: "money", good: "down", hint: "Spend divided by conversions." },
  { key: "conversionValue", label: "Conv. value", kind: "money", good: "up", hint: "Total value of conversions." },
  { key: "roas", label: "ROAS", kind: "ratio", good: "up", hint: "Conversion value divided by spend." },
];

export function kpiValue(t: Totals, key: KpiKey): number | null {
  return t[key];
}

/** A KPI for each day, derived from that day's sums, for the sparklines. */
export function kpiSeries(days: DayRow[], key: KpiKey): Array<number | null> {
  return days.map(function each(day) {
    return kpiValue(derive(day), key);
  });
}

/* ------------------------------------------------------------------ */
/* Comparison                                                          */
/* ------------------------------------------------------------------ */

export interface Delta {
  /** Percent change, or null when the previous value was zero. */
  pct: number | null;
  direction: "up" | "down" | "flat";
  /** The previous value was zero and this one is not: there is no percentage to give. */
  isNew: boolean;
}

export function deltaOf(current: number | null, previous: number | null): Delta | null {
  if (current === null || previous === null) {
    return null;
  }
  if (previous === 0) {
    if (current === 0) {
      return { pct: 0, direction: "flat", isNew: false };
    }
    return { pct: null, direction: current > 0 ? "up" : "down", isNew: true };
  }
  const pct = ((current - previous) / Math.abs(previous)) * 100;
  if (Math.abs(pct) < 0.05) {
    return { pct: 0, direction: "flat", isNew: false };
  }
  return { pct: pct, direction: pct > 0 ? "up" : "down", isNew: false };
}

/** Was the change a good one? Null for a neutral metric or no change. */
export function verdict(delta: Delta | null, good: KpiDef["good"]): "good" | "bad" | null {
  if (delta === null || good === "neutral" || delta.direction === "flat") {
    return null;
  }
  return delta.direction === good ? "good" : "bad";
}

/* ------------------------------------------------------------------ */
/* Dates                                                               */
/* ------------------------------------------------------------------ */

export type RangeId =
  | "LAST_7_DAYS"
  | "LAST_14_DAYS"
  | "LAST_30_DAYS"
  | "LAST_90_DAYS"
  | "THIS_MONTH"
  | "LAST_MONTH"
  | "CUSTOM";

export const RANGES: Array<{ id: RangeId; label: string }> = [
  { id: "LAST_7_DAYS", label: "7 days" },
  { id: "LAST_14_DAYS", label: "14 days" },
  { id: "LAST_30_DAYS", label: "30 days" },
  { id: "LAST_90_DAYS", label: "90 days" },
  { id: "THIS_MONTH", label: "This month" },
  { id: "LAST_MONTH", label: "Last month" },
  { id: "CUSTOM", label: "Custom" },
];

export interface DateWindow {
  /** Inclusive, "YYYY-MM-DD". */
  start: string;
  end: string;
  days: number;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function parseDay(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

function formatIso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function isIsoDay(value: string): boolean {
  return ISO_DAY.test(value) && !Number.isNaN(parseDay(value));
}

export function addDays(iso: string, n: number): string {
  return formatIso(parseDay(iso) + n * DAY_MS);
}

/** Days from a to b, both included. */
export function spanDays(a: string, b: string): number {
  return Math.round((parseDay(b) - parseDay(a)) / DAY_MS) + 1;
}

/**
 * Today's date in the ACCOUNT's timezone, not the browser's. Google Ads cuts
 * its days at the account's midnight, so "yesterday" for an account in Kolkata
 * is a different date from "yesterday" for a browser in California for half the
 * day. Falls back to the browser's date for a zone this runtime does not know.
 */
export function todayIn(timeZone: string | undefined, now: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone && timeZone.length > 0 ? timeZone : undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  } catch (unknownZone) {
    return new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  }
}

/**
 * The concrete dates behind a range. Every tool is sent explicit dates rather
 * than a preset, so the current window and the previous one are computed by the
 * same code and can never be off from each other by a day.
 *
 * Like Google's own presets, "last N days" ends YESTERDAY: today's numbers are
 * partial and would drag every ratio down.
 */
export function windowFor(
  range: RangeId,
  custom: { start: string; end: string },
  today: string
): DateWindow {
  const yesterday = addDays(today, -1);
  const make = function make(start: string, end: string): DateWindow {
    return { start: start, end: end, days: spanDays(start, end) };
  };

  switch (range) {
    case "LAST_7_DAYS":
      return make(addDays(yesterday, -6), yesterday);
    case "LAST_14_DAYS":
      return make(addDays(yesterday, -13), yesterday);
    case "LAST_90_DAYS":
      return make(addDays(yesterday, -89), yesterday);
    case "THIS_MONTH":
      return make(today.slice(0, 8) + "01", today);
    case "LAST_MONTH": {
      const firstOfThis = today.slice(0, 8) + "01";
      const lastOfPrev = addDays(firstOfThis, -1);
      return make(lastOfPrev.slice(0, 8) + "01", lastOfPrev);
    }
    case "CUSTOM": {
      if (isIsoDay(custom.start) && isIsoDay(custom.end)) {
        const end = custom.end > today ? today : custom.end;
        const start = custom.start > end ? end : custom.start;
        return make(start, end);
      }
      return make(addDays(yesterday, -29), yesterday);
    }
    default:
      return make(addDays(yesterday, -29), yesterday);
  }
}

/** The window of the same length that ends the day before this one starts. */
export function previousWindow(w: DateWindow): DateWindow {
  const end = addDays(w.start, -1);
  return { start: addDays(end, -(w.days - 1)), end: end, days: w.days };
}

export function eachDay(w: DateWindow): string[] {
  const out: string[] = [];
  for (let i = 0; i < w.days; i += 1) {
    out.push(addDays(w.start, i));
  }
  return out;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "12 Sep". Built by hand: toLocaleDateString shifts a UTC day by the browser's offset. */
export function dayShort(iso: string): string {
  if (!isIsoDay(iso)) {
    return iso;
  }
  const [, m, d] = iso.split("-").map(Number);
  return String(d) + " " + MONTHS[m - 1];
}

/** "Sat, 12 Sep 2026". */
export function dayLong(iso: string): string {
  if (!isIsoDay(iso)) {
    return iso;
  }
  const y = iso.slice(0, 4);
  const dow = new Date(parseDay(iso)).getUTCDay();
  return WEEKDAY_SHORT[dow] + ", " + dayShort(iso) + " " + y;
}

export function windowLabel(w: DateWindow): string {
  const sameYear = w.start.slice(0, 4) === w.end.slice(0, 4);
  return (
    dayShort(w.start) +
    (sameYear ? "" : " " + w.start.slice(0, 4)) +
    " – " +
    dayShort(w.end) +
    " " +
    w.end.slice(0, 4)
  );
}

/* ------------------------------------------------------------------ */
/* Number formatting                                                   */
/* ------------------------------------------------------------------ */

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString();
}

export function fmtCompact(n: number): string {
  if (Math.abs(n) < 1000) {
    return fmtInt(n);
  }
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

export function fmtNum(n: number, digits: number = 1): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function fmtPct(n: number, digits: number = 2): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }) + "%";
}

export function fmtRatio(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "×";
}

/**
 * Money in the account's own currency. An unknown currency code falls back to
 * the bare number with the code after it -- still correct, just plainer.
 */
export function fmtMoney(
  n: number,
  currency: string,
  options: { compact?: boolean; decimals?: number } = {}
): string {
  const decimals = options.decimals !== undefined ? options.decimals : Math.abs(n) >= 1000 ? 0 : 2;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      notation: options.compact === true && Math.abs(n) >= 100000 ? "compact" : "standard",
      minimumFractionDigits: options.compact === true && Math.abs(n) >= 100000 ? 0 : decimals,
      maximumFractionDigits: options.compact === true && Math.abs(n) >= 100000 ? 1 : decimals,
    }).format(n);
  } catch (unknownCurrency) {
    return fmtNum(n, decimals) + (currency ? " " + currency : "");
  }
}

export function fmtKpi(kind: KpiKind, value: number | null, currency: string): string {
  if (value === null) {
    return "—";
  }
  switch (kind) {
    case "money":
      return fmtMoney(value, currency, { compact: true });
    case "int":
      return fmtCompact(value);
    case "pct":
      return fmtPct(value, 2);
    case "ratio":
      return fmtRatio(value);
    default:
      return fmtNum(value, 1);
  }
}

export function fmtDelta(delta: Delta): string {
  if (delta.isNew) {
    return "New";
  }
  if (delta.pct === null || delta.direction === "flat") {
    return "No change";
  }
  return (delta.pct > 0 ? "+" : "−") + fmtNum(Math.abs(delta.pct), Math.abs(delta.pct) >= 100 ? 0 : 1) + "%";
}

/** "1234567890" -> "123-456-7890", the way Google prints a customer id. */
export function fmtCustomerId(id: string): string {
  const clean = id.replace(/\D/g, "");
  return clean.length === 10 ? clean.slice(0, 3) + "-" + clean.slice(3, 6) + "-" + clean.slice(6) : id;
}

const ACRONYMS = ["cpa", "cpc", "cpm", "roas", "ctr", "url", "tv", "id", "ai", "sku", "pmax", "api"];

/** "SEARCH_PARTNERS" -> "Search partners"; "TARGET_CPA_OPT_IN" -> "Target CPA opt in". */
export function humanize(value: string): string {
  const words = value.replace(/_/g, " ").trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return value;
  }
  return words
    .map(function word(w, i) {
      if (ACRONYMS.indexOf(w) !== -1) {
        return w.toUpperCase();
      }
      return i === 0 ? w[0].toUpperCase() + w.slice(1) : w;
    })
    .join(" ");
}

const NETWORK_NAMES: Record<string, string> = {
  SEARCH: "Google Search",
  SEARCH_PARTNERS: "Search partners",
  CONTENT: "Display Network",
  YOUTUBE_SEARCH: "YouTube search",
  YOUTUBE_WATCH: "YouTube videos",
  MIXED: "Cross-network",
  UNKNOWN: "Unknown",
  UNSPECIFIED: "Unknown",
};

export function networkName(value: string): string {
  return NETWORK_NAMES[value] !== undefined ? NETWORK_NAMES[value] : humanize(value);
}

const DEVICE_NAMES: Record<string, string> = {
  MOBILE: "Mobile",
  DESKTOP: "Desktop",
  TABLET: "Tablet",
  CONNECTED_TV: "Connected TV",
  OTHER: "Other",
  UNKNOWN: "Unknown",
  UNSPECIFIED: "Unknown",
};

export function deviceName(value: string): string {
  return DEVICE_NAMES[value] !== undefined ? DEVICE_NAMES[value] : humanize(value);
}

/* ------------------------------------------------------------------ */
/* Countries                                                           */
/* ------------------------------------------------------------------ */

/*
 * Google's country criterion ids are 2000 plus the ISO 3166-1 numeric code
 * (2840 is the United States, 2356 is India), so a country can be named without
 * a lookup call. Only the codes listed here are named; anything else shows as
 * "Country <id>" rather than a guess.
 */
const ISO_NUMERIC_TO_ALPHA2: Record<number, string> = {
  4: "AF", 8: "AL", 12: "DZ", 24: "AO", 32: "AR", 36: "AU", 40: "AT", 48: "BH", 50: "BD",
  56: "BE", 68: "BO", 76: "BR", 100: "BG", 112: "BY", 124: "CA", 144: "LK", 152: "CL",
  156: "CN", 170: "CO", 188: "CR", 191: "HR", 196: "CY", 203: "CZ", 208: "DK", 214: "DO",
  218: "EC", 818: "EG", 233: "EE", 231: "ET", 246: "FI", 250: "FR", 276: "DE", 288: "GH",
  300: "GR", 320: "GT", 344: "HK", 348: "HU", 352: "IS", 356: "IN", 360: "ID", 364: "IR",
  368: "IQ", 372: "IE", 376: "IL", 380: "IT", 392: "JP", 400: "JO", 398: "KZ", 404: "KE",
  410: "KR", 414: "KW", 422: "LB", 428: "LV", 440: "LT", 442: "LU", 458: "MY", 470: "MT",
  484: "MX", 504: "MA", 524: "NP", 528: "NL", 554: "NZ", 566: "NG", 578: "NO", 512: "OM",
  586: "PK", 591: "PA", 604: "PE", 608: "PH", 616: "PL", 620: "PT", 634: "QA", 642: "RO",
  643: "RU", 682: "SA", 688: "RS", 702: "SG", 703: "SK", 705: "SI", 710: "ZA", 724: "ES",
  752: "SE", 756: "CH", 158: "TW", 834: "TZ", 764: "TH", 788: "TN", 792: "TR", 800: "UG",
  804: "UA", 784: "AE", 826: "GB", 840: "US", 858: "UY", 862: "VE", 704: "VN",
};

export function countryName(criterionId: string): string {
  const numeric = Number(criterionId) - 2000;
  const alpha2 = ISO_NUMERIC_TO_ALPHA2[numeric];
  if (alpha2 === undefined) {
    return "Country " + criterionId;
  }
  try {
    const names = new Intl.DisplayNames(["en"], { type: "region" });
    return names.of(alpha2) || alpha2;
  } catch (unsupported) {
    return alpha2;
  }
}

/* ------------------------------------------------------------------ */
/* Parsed report rows                                                  */
/* ------------------------------------------------------------------ */

export interface DayRow extends Metrics {
  date: string;
}

/** One row per day, oldest first, with any unreadable rows dropped. */
export function parseDaily(data: Json): DayRow[] {
  const merged: Record<string, DayRow> = {};
  for (const row of rowsOf(data, "rows")) {
    const date = str(row.date);
    if (!isIsoDay(date)) {
      continue;
    }
    const m = parseMetrics(row);
    const seen = merged[date];
    merged[date] =
      seen === undefined
        ? { date: date, ...m }
        : { date: date, ...sumMetrics([seen, m]) };
  }
  return Object.keys(merged)
    .sort()
    .map(function byDate(date) {
      return merged[date];
    });
}

/** Every day of the window, with the quiet ones present at zero, so a chart's x axis does not lie. */
export function fillDays(days: DayRow[], w: DateWindow): DayRow[] {
  const by: Record<string, DayRow> = {};
  for (const day of days) {
    by[day.date] = day;
  }
  return eachDay(w).map(function each(date) {
    return (
      by[date] || {
        date: date,
        impressions: 0,
        clicks: 0,
        cost: 0,
        conversions: 0,
        conversionValue: 0,
      }
    );
  });
}

export interface CampaignRow extends Metrics {
  id: string;
  name: string;
  status: string;
}

export function parseCampaigns(data: Json): CampaignRow[] {
  const merged: Record<string, CampaignRow> = {};
  for (const row of rowsOf(data, "rows")) {
    const id = str(row.campaign_id) || str(row.campaign_name);
    if (id === "") {
      continue;
    }
    const m = parseMetrics(row);
    const seen = merged[id];
    merged[id] =
      seen === undefined
        ? { id: id, name: str(row.campaign_name) || "Campaign " + id, status: str(row.status), ...m }
        : { ...seen, ...sumMetrics([seen, m]) };
  }
  return Object.keys(merged).map(function each(id) {
    return merged[id];
  });
}

export interface SegmentRow extends Metrics {
  key: string;
}

/** Rows keyed by one segment field ("device", "adNetworkType", "hour", "dayOfWeek"). */
export function parseSegment(data: Json, field: string): SegmentRow[] {
  const merged: Record<string, SegmentRow> = {};
  for (const row of rowsOf(data, "rows")) {
    const key = str(row[field]);
    if (key === "") {
      continue;
    }
    const m = parseMetrics(row);
    const seen = merged[key];
    merged[key] = seen === undefined ? { key: key, ...m } : { key: key, ...sumMetrics([seen, m]) };
  }
  return Object.keys(merged).map(function each(key) {
    return merged[key];
  });
}

export interface GeoRow extends Metrics {
  id: string;
  name: string;
}

/**
 * Countries by spend. The tool returns one row per country and location type;
 * a person's physical location and their location of interest measure the same
 * impressions two ways, so adding them would count everything twice. Physical
 * presence is used when the account has it, and everything otherwise.
 */
export function parseGeo(data: Json): { rows: GeoRow[]; basis: string } {
  const all = rowsOf(data, "rows");
  const present = all.filter(function isPresence(row) {
    return str(row.location_type) === "LOCATION_OF_PRESENCE";
  });
  const use = present.length > 0 ? present : all;
  const merged: Record<string, GeoRow> = {};
  for (const row of use) {
    const id = str(row.country_criterion_id);
    if (id === "") {
      continue;
    }
    const m = parseMetrics(row);
    const seen = merged[id];
    merged[id] =
      seen === undefined
        ? { id: id, name: countryName(id), ...m }
        : { ...seen, ...sumMetrics([seen, m]) };
  }
  return {
    rows: Object.keys(merged)
      .map(function each(id) {
        return merged[id];
      })
      .sort(function bySpend(a, b) {
        return b.cost - a.cost;
      }),
    basis: present.length > 0 ? "Where people physically were" : "All locations",
  };
}

export interface KeywordRow extends Metrics {
  id: string;
  text: string;
  matchType: string;
  qualityScore: number | null;
  adGroup: string;
  campaign: string;
}

export function parseKeywords(data: Json): KeywordRow[] {
  return rowsOf(data, "rows").map(function toRow(row) {
    const qs = row.quality_score;
    return {
      id: str(row.criterion_id) + "/" + str(row.ad_group_id),
      text: str(row.text),
      matchType: str(row.match_type),
      qualityScore: typeof qs === "number" ? qs : null,
      adGroup: str(row.ad_group_name),
      campaign: str(row.campaign_name),
      ...parseMetrics(row),
    };
  });
}

export interface TermRow extends Metrics {
  term: string;
  matchType: string;
  campaign: string;
  adGroup: string;
  costPerConv: number | null;
  roas: number | null;
}

export interface SearchTerms {
  examined: number;
  totalCost: number;
  wastedCost: number;
  wastePercent: number;
  wasters: TermRow[];
  winners: TermRow[];
}

function toTerm(row: Record<string, Json>): TermRow {
  const cpc = row.cost_per_conversion;
  const roas = row.roas;
  return {
    term: str(row.term),
    matchType: str(row.match_type),
    campaign: str(row.campaign),
    adGroup: str(row.ad_group),
    costPerConv: typeof cpc === "number" ? cpc : null,
    roas: typeof roas === "number" ? roas : null,
    ...parseMetrics(row),
  };
}

export function parseSearchTerms(data: Json): SearchTerms | null {
  if (!isRecord(data) || !isRecord(data.summary)) {
    return null;
  }
  const s = data.summary;
  return {
    examined: num(s.terms_examined),
    totalCost: num(s.total_cost),
    wastedCost: num(s.wasted_cost),
    wastePercent: num(s.waste_percent),
    wasters: rowsOf(data, "top_wasters").map(toTerm),
    winners: rowsOf(data, "top_winners").map(toTerm),
  };
}

export interface QualityRow {
  id: string;
  keyword: string;
  matchType: string;
  score: number | null;
  expectedCtr: string;
  adRelevance: string;
  landingPage: string;
  adGroup: string;
  campaign: string;
}

export interface QualityReport {
  scored: number;
  unscored: number;
  poor: number;
  average: number;
  great: number;
  rows: QualityRow[];
}

export function parseQuality(data: Json): QualityReport | null {
  if (!isRecord(data) || !isRecord(data.summary)) {
    return null;
  }
  const s = data.summary;
  return {
    scored: num(s.scored_keywords),
    unscored: num(s.unscored_keywords),
    poor: num(s.poor_qs_1_4),
    average: num(s.average_qs_5_7),
    great: num(s.great_qs_8_10),
    rows: rowsOf(data, "keywords").map(function toRow(row) {
      const score = row.quality_score;
      return {
        id: str(row.criterion_id),
        keyword: str(row.keyword),
        matchType: str(row.match_type),
        score: typeof score === "number" ? score : null,
        expectedCtr: str(row.expected_ctr),
        adRelevance: str(row.ad_relevance),
        landingPage: str(row.landing_page_experience),
        adGroup: str(row.ad_group),
        campaign: str(row.campaign),
      };
    }),
  };
}

export interface ImpressionShareRow {
  id: string;
  name: string;
  share: number;
  lostBudget: number;
  lostRank: number;
  topShare: number;
  absTopShare: number;
  cost: number;
}

export function parseImpressionShare(data: Json): ImpressionShareRow[] {
  return rowsOf(data, "rows").map(function toRow(row) {
    return {
      id: str(row.campaign_id),
      name: str(row.campaign_name),
      share: num(row.impression_share),
      lostBudget: num(row.lost_is_budget),
      lostRank: num(row.lost_is_rank),
      topShare: num(row.top_is),
      absTopShare: num(row.abs_top_is),
      cost: num(row.cost),
    };
  });
}

export interface PacingRow {
  id: string;
  name: string;
  status: string;
  dailyBudget: number;
  mtdSpend: number;
  expectedMtd: number;
  targetEom: number;
  projectedEom: number;
  variance: number;
  pace: string;
}

export interface Pacing {
  asOf: string;
  daysElapsed: number;
  rows: PacingRow[];
}

export function parsePacing(data: Json): Pacing | null {
  if (!isRecord(data)) {
    return null;
  }
  return {
    asOf: str(data.as_of),
    daysElapsed: num(data.days_elapsed),
    rows: rowsOf(data, "campaigns").map(function toRow(row) {
      return {
        id: str(row.campaign_id),
        name: str(row.campaign_name),
        status: str(row.status),
        dailyBudget: num(row.daily_budget),
        mtdSpend: num(row.mtd_spend),
        expectedMtd: num(row.expected_mtd_at_daily),
        targetEom: num(row.target_eom_spend),
        projectedEom: num(row.projected_eom_spend),
        variance: num(row.variance),
        pace: str(row.pace),
      };
    }),
  };
}

export interface ConversionActionRow {
  name: string;
  category: string;
  conversions: number;
  value: number;
  allConversions: number;
  allValue: number;
}

export function parseConversionActions(data: Json): ConversionActionRow[] {
  const merged: Record<string, ConversionActionRow> = {};
  for (const row of rowsOf(data, "rows")) {
    const name = str(row.name) || "Unnamed action";
    const seen = merged[name];
    const next = {
      name: name,
      category: str(row.category),
      conversions: num(row.conversions),
      value: num(row.conversion_value),
      allConversions: num(row.all_conversions),
      allValue: num(row.all_conversion_value),
    };
    merged[name] =
      seen === undefined
        ? next
        : {
            ...seen,
            conversions: seen.conversions + next.conversions,
            value: seen.value + next.value,
            allConversions: seen.allConversions + next.allConversions,
            allValue: seen.allValue + next.allValue,
          };
  }
  return Object.keys(merged)
    .map(function each(name) {
      return merged[name];
    })
    .sort(function byConversions(a, b) {
      return b.allConversions - a.allConversions;
    });
}

export interface Health {
  lowQuality: Array<{ id: string; text: string; score: number; adGroup: string; campaign: string }>;
  disapproved: Array<{ id: string; status: string; adGroup: string; campaign: string }>;
  paused: Array<{ id: string; name: string }>;
}

export function parseHealth(data: Json): Health | null {
  if (!isRecord(data)) {
    return null;
  }
  return {
    lowQuality: rowsOf(data, "low_quality_keywords").map(function toRow(row) {
      return {
        id: str(row.criterion_id),
        text: str(row.text),
        score: num(row.quality_score),
        adGroup: str(row.ad_group),
        campaign: str(row.campaign),
      };
    }),
    disapproved: rowsOf(data, "disapproved_ads").map(function toRow(row) {
      return {
        id: str(row.ad_id),
        status: str(row.approval_status),
        adGroup: str(row.ad_group),
        campaign: str(row.campaign),
      };
    }),
    paused: rowsOf(data, "paused_campaigns").map(function toRow(row) {
      return { id: str(row.campaign_id), name: str(row.campaign_name) };
    }),
  };
}

export interface Recommendations {
  total: number;
  types: Array<{ type: string; count: number }>;
}

export function parseRecommendations(data: Json): Recommendations | null {
  if (!isRecord(data)) {
    return null;
  }
  return {
    total: num(data.total_recommendations),
    types: rowsOf(data, "type_breakdown").map(function toRow(row) {
      return { type: str(row.type), count: num(row.count) };
    }),
  };
}

export interface ChangeEvent {
  when: string;
  user: string;
  clientType: string;
  resourceType: string;
  operation: string;
  fields: string;
  campaign: string;
}

/** A resource name like "customers/123/campaigns/456" -> "campaign 456". */
function shortResource(name: string): string {
  const parts = name.split("/");
  if (parts.length >= 4) {
    return parts[parts.length - 2].replace(/s$/, "") + " " + parts[parts.length - 1];
  }
  return name;
}

function flatten(value: Json): string {
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(flatten).filter(Boolean).join(", ");
  }
  if (isRecord(value) && Array.isArray(value.paths)) {
    return value.paths.map(flatten).filter(Boolean).join(", ");
  }
  return "";
}

export function parseChanges(data: Json): ChangeEvent[] {
  return rowsOf(data, "events").map(function toRow(row) {
    return {
      when: str(row.when),
      user: str(row.user),
      clientType: str(row.client_type),
      resourceType: str(row.resource_type),
      operation: str(row.operation),
      fields: flatten(row.changed_fields),
      campaign: shortResource(str(row.campaign)),
    };
  });
}

/* ------------------------------------------------------------------ */
/* Series helpers                                                      */
/* ------------------------------------------------------------------ */

/** A trailing average, so a spiky day sits against its own recent normal. */
export function movingAverage(values: number[], window: number): Array<number | null> {
  return values.map(function each(_, i) {
    if (i < window - 1) {
      return null;
    }
    let sum = 0;
    for (let k = i - window + 1; k <= i; k += 1) {
      sum += values[k];
    }
    return sum / window;
  });
}

/**
 * Indexes more than two standard deviations above the series' own mean. The
 * same idea as the activity charts' spike rule, on a metric that is money or a
 * count of clicks rather than a count of calls: the baseline is this account's
 * own normal, and a week of data is the least that makes "normal" mean
 * anything.
 */
export function findSpikes(values: number[]): number[] {
  if (values.length < 7) {
    return [];
  }
  const mean = values.reduce(function add(a, b) {
    return a + b;
  }, 0) / values.length;
  let variance = 0;
  for (const v of values) {
    variance += (v - mean) * (v - mean);
  }
  const sigma = Math.sqrt(variance / values.length);
  if (sigma === 0) {
    return [];
  }
  const out: number[] = [];
  values.forEach(function check(v, i) {
    if (v > 0 && v > mean + 2 * sigma) {
      out.push(i);
    }
  });
  return out;
}

/* ------------------------------------------------------------------ */
/* Highlights                                                          */
/* ------------------------------------------------------------------ */

export interface Highlight {
  tone: "good" | "bad" | "info";
  text: string;
}

/**
 * Plain-language findings, written from the numbers on the page and nothing
 * else. Each sentence is a rule over data that was fetched; when the data for
 * a rule is missing the sentence is simply not written.
 */
export function buildHighlights(input: {
  current: Totals;
  previous: Totals | null;
  campaigns: CampaignRow[];
  days: DayRow[];
  currency: string;
}): Highlight[] {
  const out: Highlight[] = [];
  const { current, previous, campaigns, days, currency } = input;
  const money = function money(n: number): string {
    return fmtMoney(n, currency, { compact: true });
  };

  if (previous !== null) {
    const spend = deltaOf(current.cost, previous.cost);
    const conv = deltaOf(current.conversions, previous.conversions);
    if (spend !== null && conv !== null && spend.pct !== null && conv.pct !== null) {
      if (spend.direction === "up" && conv.direction === "down") {
        out.push({
          tone: "bad",
          text:
            "Spend rose " + fmtNum(Math.abs(spend.pct)) + "% but conversions fell " +
            fmtNum(Math.abs(conv.pct)) + "% against the previous period.",
        });
      } else if (spend.direction === "down" && conv.direction === "up") {
        out.push({
          tone: "good",
          text:
            "Conversions rose " + fmtNum(Math.abs(conv.pct)) + "% on " +
            fmtNum(Math.abs(spend.pct)) + "% less spend than the previous period.",
        });
      } else if (spend.direction === "up" && conv.direction === "up") {
        out.push({
          tone: "info",
          text:
            "Spend is up " + fmtNum(Math.abs(spend.pct)) + "% and conversions are up " +
            fmtNum(Math.abs(conv.pct)) + "% against the previous period.",
        });
      } else if (spend.direction === "down" && conv.direction === "down") {
        out.push({
          tone: "info",
          text:
            "Spend is down " + fmtNum(Math.abs(spend.pct)) + "% and conversions are down " +
            fmtNum(Math.abs(conv.pct)) + "% against the previous period.",
        });
      }
    }
    const cpa = deltaOf(current.costPerConv, previous.costPerConv);
    if (cpa !== null && cpa.pct !== null && Math.abs(cpa.pct) >= 10) {
      out.push({
        tone: cpa.direction === "down" ? "good" : "bad",
        text:
          "Cost per conversion " + (cpa.direction === "down" ? "improved" : "worsened") +
          " " + fmtNum(Math.abs(cpa.pct)) + "% to " + money(current.costPerConv || 0) + ".",
      });
    }
  }

  const spending = campaigns.filter(function spent(c) {
    return c.cost > 0;
  });
  const totalSpend = spending.reduce(function add(sum, c) {
    return sum + c.cost;
  }, 0);
  if (spending.length > 1 && totalSpend > 0) {
    const top = spending.slice().sort(function bySpend(a, b) {
      return b.cost - a.cost;
    })[0];
    const share = (top.cost / totalSpend) * 100;
    if (share >= 40) {
      out.push({
        tone: "info",
        text: "“" + top.name + "” takes " + fmtNum(share, 0) + "% of spend (" + money(top.cost) + ").",
      });
    }
  }

  const dead = spending.filter(function noConv(c) {
    return c.conversions === 0 && c.cost > 0;
  });
  if (dead.length > 0 && current.conversions > 0) {
    const deadSpend = dead.reduce(function add(sum, c) {
      return sum + c.cost;
    }, 0);
    out.push({
      tone: "bad",
      text:
        String(dead.length) + (dead.length === 1 ? " campaign spent " : " campaigns spent ") +
        money(deadSpend) + " without a single conversion.",
    });
  }

  const spikeIndexes = findSpikes(
    days.map(function cost(d) {
      return d.cost;
    })
  );
  if (spikeIndexes.length > 0) {
    const worst = spikeIndexes.slice().sort(function bySpend(a, b) {
      return days[b].cost - days[a].cost;
    })[0];
    out.push({
      tone: "info",
      text:
        "Spend spiked on " + dayLong(days[worst].date) + " to " + money(days[worst].cost) +
        " (" + fmtNum(days[worst].cost / Math.max(current.cost / Math.max(days.length, 1), 0.0001), 1) +
        "× the daily average).",
    });
  }

  return out;
}

/* ------------------------------------------------------------------ */
/* CSV                                                                 */
/* ------------------------------------------------------------------ */

/**
 * RFC 4180 CSV. A text cell that starts with =, +, - or @ is prefixed with an
 * apostrophe: a search term or campaign name is somebody else's text, and a
 * spreadsheet would run it as a formula.
 */
export function toCsv(headers: string[], rows: Array<Array<string | number | null>>): string {
  const cell = function cell(value: string | number | null): string {
    if (value === null) {
      return "";
    }
    let text = String(value);
    if (typeof value === "string" && /^[=+\-@\t\r]/.test(text)) {
      text = "'" + text;
    }
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  };
  return [headers.map(cell).join(",")]
    .concat(
      rows.map(function line(row) {
        return row.map(cell).join(",");
      })
    )
    .join("\r\n");
}

export function downloadCsv(filename: string, csv: string): void {
  // The byte-order mark is what makes Excel read the file as UTF-8.
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
