"use client";

/**
 * The Google Ads report for one connection.
 *
 * Everything on it is read through the connection's own tools, by one batch
 * request per view (see useReportPack), so it shows exactly what an AI client
 * connected to the same URL could see -- no separate data path, no stored
 * copy, nothing invented.
 *
 * The controls choose an ACCOUNT and a DATE RANGE. A connection usually spans
 * several Google Ads accounts (a manager account and the clients under it), so
 * the account list is fetched first -- through list_accounts, cached on the
 * server for hours -- and each report is asked for one client account, logging
 * in through its manager where there is one.
 *
 * The range is turned into concrete dates in the ACCOUNT's time zone, and the
 * comparison period is the same number of days immediately before, computed by
 * the same code (see ads-model). Health and month-to-date views ignore the
 * range, and changing it does not refetch them.
 *
 * Choices are remembered per browser (localStorage, guarded: a private window
 * or blocked storage just means it starts from defaults).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { ChartColumn, Clock, HeartPulse, Layers, RefreshCw, Search } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  RANGES,
  fmtCustomerId,
  isRecord,
  isIsoDay,
  previousWindow,
  rowsOf,
  str,
  todayIn,
  windowFor,
  windowLabel,
} from "@/components/reports/google-ads/ads-model";
import type { RangeId } from "@/components/reports/google-ads/ads-model";
import { PACKS, buildRuns, packKey } from "@/components/reports/google-ads/ads-packs";
import type { PackArgs, PackId, ReportCtx } from "@/components/reports/google-ads/ads-packs";
import { Card, Segmented, Skeleton } from "@/components/reports/google-ads/ads-ui";
import { sliceOf, usePack } from "@/components/reports/google-ads/useReportPack";
import OverviewPanel from "@/components/reports/google-ads/OverviewPanel";
import CampaignsPanel from "@/components/reports/google-ads/CampaignsPanel";
import SearchPanel from "@/components/reports/google-ads/SearchPanel";
import TimingPanel from "@/components/reports/google-ads/TimingPanel";
import HealthPanel from "@/components/reports/google-ads/HealthPanel";
import type { Connection } from "@/lib/api";
import "@/components/reports/google-ads/google-ads-report.css";

const PACK_ICONS: Record<PackId, LucideIcon> = {
  overview: ChartColumn,
  campaigns: Layers,
  search: Search,
  timing: Clock,
  health: HeartPulse,
};

/* ------------------------------------------------------------------ */
/* Accounts                                                            */
/* ------------------------------------------------------------------ */

interface Account {
  id: string;
  name: string;
  currency: string;
  timeZone: string;
  status: string;
  test: boolean;
  /** The manager account to log in through, or null for direct access. */
  loginCustomerId: string | null;
  managerName: string;
}

/** Client accounts only: a manager has no campaigns of its own to report on. */
function parseAccounts(data: unknown): Account[] {
  const all = rowsOf(data, "all_accounts");
  const managers: Record<string, string> = {};
  for (const row of all) {
    if (row.manager === true) {
      managers[str(row.id)] = str(row.name);
    }
  }
  const out: Account[] = [];
  for (const row of all) {
    if (row.manager === true) {
      continue;
    }
    const login = str(row.login_customer_id);
    out.push({
      id: str(row.id),
      name: str(row.name),
      currency: str(row.currency) || "USD",
      timeZone: str(row.time_zone),
      status: str(row.status),
      test: row.test_account === true,
      loginCustomerId: login === "" ? null : login,
      managerName: login === "" ? "" : managers[login] || "",
    });
  }
  return out;
}

function remembered(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch (unavailable) {
    return null;
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch (unavailable) {
    // Not remembering is fine; the report still works.
  }
}

function isRange(value: string | null): value is RangeId {
  return value !== null && RANGES.some(function is(r) {
    return r.id === value;
  });
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default function GoogleAdsReport({ connection }: { connection: Connection }) {
  const storeKey = "hc.ads." + String(connection.id) + ".";

  const [customerId, setCustomerId] = useState<string | null>(null);
  const [manual, setManual] = useState<{ id: string; login: string | null } | null>(null);
  const [manualText, setManualText] = useState<string>("");
  const [range, setRange] = useState<RangeId>("LAST_30_DAYS");
  const [custom, setCustom] = useState<{ start: string; end: string }>({ start: "", end: "" });
  const [compare, setCompare] = useState<boolean>(true);
  const [pack, setPack] = useState<PackId>("overview");
  const restoredRef = useRef<boolean>(false);
  const tabsRef = useRef<HTMLDivElement | null>(null);

  // Restore the last choices once, on the client.
  useEffect(
    function restore() {
      if (restoredRef.current) {
        return;
      }
      restoredRef.current = true;
      const savedRange = remembered("hc.ads.range");
      if (isRange(savedRange)) {
        setRange(savedRange);
      }
      const savedStart = remembered("hc.ads.custom.start");
      const savedEnd = remembered("hc.ads.custom.end");
      if (savedStart !== null && savedEnd !== null && isIsoDay(savedStart) && isIsoDay(savedEnd)) {
        setCustom({ start: savedStart, end: savedEnd });
      }
      const savedCompare = remembered("hc.ads.compare");
      if (savedCompare !== null) {
        setCompare(savedCompare === "1");
      }
      const savedAccount = remembered(storeKey + "account");
      if (savedAccount !== null) {
        setCustomerId(savedAccount);
      }
    },
    [storeKey]
  );

  /* ---- Accounts ---- */

  const accountsPack = usePack(connection.id, "accounts", [
    { id: "accounts", tool: "list_accounts", args: {} },
  ]);
  const accountsSlice = sliceOf(accountsPack.state, "accounts");
  const accounts = useMemo(
    function list() {
      return accountsSlice.status === "ready" ? parseAccounts(accountsSlice.data) : [];
    },
    [accountsSlice.status, accountsSlice.data]
  );
  const accountErrors = useMemo(
    function errors() {
      return accountsSlice.status === "ready" && isRecord(accountsSlice.data)
        ? rowsOf(accountsSlice.data, "errors").map(function message(row) {
            return str(row.error);
          })
        : [];
    },
    [accountsSlice.status, accountsSlice.data]
  );

  // Pick a default once the list is here: an enabled real account first.
  useEffect(
    function chooseDefault() {
      if (accountsSlice.status !== "ready" || manual !== null || accounts.length === 0) {
        return;
      }
      const stillThere =
        customerId !== null &&
        accounts.some(function is(a) {
          return a.id === customerId;
        });
      if (stillThere) {
        return;
      }
      const pick =
        accounts.filter(function real(a) {
          return a.status === "ENABLED" && !a.test;
        })[0] ||
        accounts.filter(function enabled(a) {
          return a.status === "ENABLED";
        })[0] ||
        accounts[0];
      setCustomerId(pick.id);
    },
    [accountsSlice.status, accounts, customerId, manual]
  );

  const account: Account | null =
    manual !== null
      ? {
          id: manual.id,
          name: "",
          currency: "USD",
          timeZone: "",
          status: "",
          test: false,
          loginCustomerId: manual.login,
          managerName: "",
        }
      : accounts.filter(function is(a) {
          return a.id === customerId;
        })[0] || null;

  /* ---- Windows ---- */

  const today = todayIn(account !== null ? account.timeZone : undefined);
  const win = useMemo(
    function window() {
      return windowFor(range, custom, today);
    },
    [range, custom, today]
  );
  const prevWin = useMemo(
    function previous() {
      return previousWindow(win);
    },
    [win]
  );

  /* ---- The pack for the sub-tab on screen ---- */

  const args: PackArgs | null =
    account === null
      ? null
      : {
          customerId: account.id,
          loginCustomerId: account.loginCustomerId,
          window: win,
          prevWindow: prevWin,
          compare: compare,
        };
  const runs = args === null ? [] : buildRuns(pack, args);
  const key = args === null ? null : packKey(pack, args);
  const view = usePack(connection.id, key, runs);

  const ctx: ReportCtx = {
    currency: account !== null ? account.currency : "USD",
    timeZone: account !== null ? account.timeZone : "",
    window: win,
    prevWindow: prevWin,
    compare: compare,
    slice: function slice(id: string) {
      return sliceOf(view.state, id);
    },
    retry: view.reload,
    goTo: setPack,
  };

  /* ---- Handlers ---- */

  function chooseAccount(id: string): void {
    setManual(null);
    setCustomerId(id);
    remember(storeKey + "account", id);
  }

  function chooseRange(next: RangeId): void {
    if (next === "CUSTOM" && custom.start === "") {
      setCustom({ start: win.start, end: win.end });
    }
    setRange(next);
    remember("hc.ads.range", next);
  }

  function setCustomDate(field: "start" | "end", value: string): void {
    const next = { ...custom, [field]: value };
    setCustom(next);
    if (isIsoDay(next.start) && isIsoDay(next.end)) {
      remember("hc.ads.custom.start", next.start);
      remember("hc.ads.custom.end", next.end);
    }
  }

  function toggleCompare(next: boolean): void {
    setCompare(next);
    remember("hc.ads.compare", next ? "1" : "0");
  }

  function openManualId(): void {
    const clean = manualText.replace(/\D/g, "");
    if (clean.length !== 10) {
      return;
    }
    setManual({ id: clean, login: null });
  }

  function onTabKey(event: KeyboardEvent<HTMLDivElement>): void {
    const index = PACKS.findIndex(function is(p) {
      return p.id === pack;
    });
    let next = -1;
    if (event.key === "ArrowRight") {
      next = (index + 1) % PACKS.length;
    } else if (event.key === "ArrowLeft") {
      next = (index - 1 + PACKS.length) % PACKS.length;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = PACKS.length - 1;
    }
    if (next === -1) {
      return;
    }
    event.preventDefault();
    setPack(PACKS[next].id);
    const button = tabsRef.current?.querySelector<HTMLButtonElement>("#ga-tab-" + PACKS[next].id);
    button?.focus();
  }

  /* ---- Render ---- */

  const loading = view.state.status === "loading";
  const updated = view.state.loadedAt === null ? null : new Date(view.state.loadedAt);

  // No account list and none chosen by hand: say why, and offer the way round.
  if (accountsSlice.status === "loading") {
    return (
      <div className="ga">
        <Skeleton height={64} />
        <Skeleton height={320} />
      </div>
    );
  }

  if (manual === null && (accountsSlice.status === "error" || accounts.length === 0)) {
    const detail =
      accountsSlice.status === "error"
        ? accountsSlice.error
        : accountErrors.length > 0
        ? accountErrors[0]
        : "No client accounts were found under this connection.";
    return (
      <div className="ga">
        <Card title="Choose an account" note="The report needs a Google Ads client account to read from.">
          <div className="ga-error" role="alert">
            <p className="ga-error-text">{detail}</p>
            <p className="ga-error-hint">
              If you know the account&rsquo;s 10-digit customer ID (top right of Google Ads), enter it to open the report directly.
            </p>
          </div>
          <form
            className="ga-manual"
            onSubmit={function submit(event) {
              event.preventDefault();
              openManualId();
            }}
          >
            <label className="ga-field">
              <span>Customer ID</span>
              <input
                type="text"
                inputMode="numeric"
                placeholder="123-456-7890"
                value={manualText}
                onChange={function change(event) {
                  setManualText(event.target.value);
                }}
              />
            </label>
            <button type="submit" className="ga-btn is-primary" disabled={manualText.replace(/\D/g, "").length !== 10}>
              Open report
            </button>
            <button type="button" className="ga-btn" onClick={accountsPack.reload}>
              <RefreshCw size={13} strokeWidth={2} aria-hidden="true" />
              <span>Reload accounts</span>
            </button>
          </form>
        </Card>
      </div>
    );
  }

  // Group the options under the manager they are reached through.
  const groups: Array<{ label: string; items: Account[] }> = [];
  for (const a of accounts) {
    const label = a.loginCustomerId === null ? "Direct access" : "Via " + (a.managerName || fmtCustomerId(a.loginCustomerId));
    const found = groups.filter(function is(g) {
      return g.label === label;
    })[0];
    if (found !== undefined) {
      found.items.push(a);
    } else {
      groups.push({ label: label, items: [a] });
    }
  }

  return (
    <div className="ga">
      <div className="ga-toolbar">
        <div className="ga-toolbar-row">
          {manual === null ? (
            <label className="ga-field">
              <span>Account</span>
              <select
                value={customerId === null ? "" : customerId}
                onChange={function change(event) {
                  chooseAccount(event.target.value);
                }}
              >
                {groups.map(function group(g) {
                  return (
                    <optgroup key={g.label} label={g.label}>
                      {g.items.map(function option(a) {
                        return (
                          <option key={a.id} value={a.id}>
                            {(a.name || "Account") + " · " + fmtCustomerId(a.id) + " · " + a.currency +
                              (a.test ? " · test" : "") +
                              (a.status !== "" && a.status !== "ENABLED" ? " · " + a.status.toLowerCase() : "")}
                          </option>
                        );
                      })}
                    </optgroup>
                  );
                })}
              </select>
            </label>
          ) : (
            <div className="ga-field">
              <span>Account</span>
              <span className="ga-static">
                {fmtCustomerId(manual.id)}
                <button type="button" className="ga-link" onClick={function back() {
                  setManual(null);
                }}>
                  Change
                </button>
              </span>
            </div>
          )}

          <div className="ga-field">
            <span>Date range</span>
            <Segmented<RangeId>
              label="Date range"
              small
              value={range}
              onChange={chooseRange}
              options={RANGES.map(function o(r) {
                return { id: r.id, label: r.label };
              })}
            />
          </div>

          <div className="ga-toolbar-end">
            <label className="ga-toggle">
              <input
                type="checkbox"
                checked={compare}
                onChange={function change(event) {
                  toggleCompare(event.target.checked);
                }}
              />
              <span className="ga-toggle-track" aria-hidden="true" />
              <span>Compare</span>
            </label>
            <button
              type="button"
              className="ga-btn"
              onClick={view.reload}
              disabled={loading || key === null}
              title="Fetch fresh numbers from Google Ads"
            >
              <RefreshCw size={13} strokeWidth={2} aria-hidden="true" className={loading ? "ga-spin" : undefined} />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {range === "CUSTOM" ? (
          <div className="ga-toolbar-row is-custom">
            <label className="ga-field">
              <span>From</span>
              <input
                type="date"
                value={custom.start}
                max={today}
                onChange={function change(event) {
                  setCustomDate("start", event.target.value);
                }}
              />
            </label>
            <label className="ga-field">
              <span>To</span>
              <input
                type="date"
                value={custom.end}
                max={today}
                onChange={function change(event) {
                  setCustomDate("end", event.target.value);
                }}
              />
            </label>
          </div>
        ) : null}

        <p className="ga-window" aria-live="polite">
          <b>{windowLabel(win)}</b>
          {" · " + String(win.days) + (win.days === 1 ? " day" : " days")}
          {compare ? " · compared with " + windowLabel(prevWin) : ""}
          {account !== null && account.timeZone !== "" ? " · " + account.timeZone.replace(/_/g, " ") : ""}
          {account !== null && account.test ? " · test account" : ""}
          {updated !== null ? " · updated " + updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
        </p>
      </div>

      <div
        className="ga-tabs"
        role="tablist"
        aria-label="Report sections"
        ref={tabsRef}
        onKeyDown={onTabKey}
      >
        {PACKS.map(function tab(p) {
          const Icon = PACK_ICONS[p.id];
          const selected = p.id === pack;
          return (
            <button
              key={p.id}
              type="button"
              id={"ga-tab-" + p.id}
              role="tab"
              className="ga-tab"
              aria-selected={selected}
              aria-controls={"ga-panel-" + p.id}
              tabIndex={selected ? 0 : -1}
              onClick={function pick() {
                setPack(p.id);
              }}
            >
              <Icon size={15} strokeWidth={1.9} aria-hidden="true" />
              <span>{p.label}</span>
            </button>
          );
        })}
      </div>

      <div id={"ga-panel-" + pack} role="tabpanel" aria-labelledby={"ga-tab-" + pack} tabIndex={-1}>
        {pack === "overview" ? <OverviewPanel ctx={ctx} /> : null}
        {pack === "campaigns" ? <CampaignsPanel ctx={ctx} /> : null}
        {pack === "search" ? <SearchPanel ctx={ctx} /> : null}
        {pack === "timing" ? <TimingPanel ctx={ctx} /> : null}
        {pack === "health" ? <HealthPanel ctx={ctx} /> : null}
      </div>

      <p className="ga-footer">
        Read live from Google Ads through this connection and cached for a few minutes. Nothing here changes your account.
      </p>
    </div>
  );
}
