"use client";

/**
 * Health & changes: what is broken, what Google suggests, and what somebody
 * changed recently.
 *
 * Health is a checklist, and it says "nothing found" plainly when that is the
 * answer: an empty red-flag list is a result, not a missing section. The change
 * history is the audit trail -- when a number moved and nobody knows why, the
 * first question is who touched the account.
 */

import { CircleCheck, TriangleAlert } from "lucide-react";
import {
  fmtNum,
  humanize,
  noteOf,
  parseChanges,
  parseHealth,
  parseRecommendations,
} from "@/components/reports/google-ads/ads-model";
import type { ChangeEvent } from "@/components/reports/google-ads/ads-model";
import { Card, DataTable, Empty, Pill, ShareBar, Slot } from "@/components/reports/google-ads/ads-ui";
import type { ReportCtx } from "@/components/reports/google-ads/ads-packs";

/** The health check caps each list; a list at its cap is "at least", not "exactly". */
const HEALTH_CAP = 50;

function count(n: number): string {
  return n >= HEALTH_CAP ? String(HEALTH_CAP) + "+" : String(n);
}

function Tile({
  label,
  value,
  tone,
  detail,
}: {
  label: string;
  value: string;
  tone: "good" | "bad" | "warn" | "muted";
  detail: string;
}) {
  return (
    <li className={"ga-health-tile is-" + tone}>
      <span className="ga-health-icon" aria-hidden="true">
        {tone === "good" ? <CircleCheck size={18} strokeWidth={2} /> : <TriangleAlert size={18} strokeWidth={2} />}
      </span>
      <span className="ga-health-value">{value}</span>
      <span className="ga-health-label">{label}</span>
      <span className="ga-health-detail">{detail}</span>
    </li>
  );
}

/** "2026-09-25 14:03:22.000000" -> "2026-09-25 14:03". */
function whenLabel(raw: string): string {
  return raw.length >= 16 ? raw.slice(0, 16) : raw;
}

function operationTone(op: string): "good" | "bad" | "warn" | "muted" {
  return op === "CREATE" ? "good" : op === "REMOVE" ? "bad" : op === "UPDATE" ? "warn" : "muted";
}

export default function HealthPanel({ ctx }: { ctx: ReportCtx }) {
  return (
    <div className="ga-stack">
      <Card title="Account health" note="Problems that cost money or reach, found by scanning the account right now." wide>
        <Slot slice={ctx.slice("health")} onRetry={ctx.retry} height={200}>
          {function render(payload) {
            const health = parseHealth(payload);
            if (health === null) {
              return <Empty>{noteOf(payload) || "The health check returned nothing."}</Empty>;
            }
            const issues = health.disapproved.length + health.lowQuality.length;
            return (
              <>
                <p className={issues === 0 ? "ga-banner is-good" : "ga-banner is-bad"} role="status">
                  {issues === 0
                    ? "No disapproved ads or low-quality keywords found."
                    : String(issues) + (issues === 1 ? " issue needs" : " issues need") + " attention."}
                </p>
                <ul className="ga-health">
                  <Tile
                    label="Disapproved ads"
                    value={count(health.disapproved.length)}
                    tone={health.disapproved.length > 0 ? "bad" : "good"}
                    detail={health.disapproved.length > 0 ? "Not running until fixed" : "All ads approved"}
                  />
                  <Tile
                    label="Poor Quality Score keywords"
                    value={count(health.lowQuality.length)}
                    tone={health.lowQuality.length > 0 ? "warn" : "good"}
                    detail={health.lowQuality.length > 0 ? "Score 4 or below" : "None at 4 or below"}
                  />
                  <Tile
                    label="Paused campaigns"
                    value={count(health.paused.length)}
                    tone="muted"
                    detail="Not spending; check they are meant to be off"
                  />
                </ul>

                {health.disapproved.length > 0 ? (
                  <>
                    <h4 className="ga-subhead">Disapproved ads</h4>
                    <DataTable
                      rows={health.disapproved}
                      caption="Disapproved ads"
                      rowKey={function key(r) {
                        return r.id + "|" + r.adGroup;
                      }}
                      pageSize={10}
                      exportName="google-ads-disapproved-ads"
                      columns={[
                        {
                          id: "ad",
                          label: "Ad ID",
                          sort: function s(r) {
                            return r.id;
                          },
                          cell: function c(r) {
                            return <span className="ga-mono">{r.id}</span>;
                          },
                          csv: function v(r) {
                            return r.id;
                          },
                        },
                        {
                          id: "status",
                          label: "Status",
                          cell: function c(r) {
                            return <Pill tone="bad">{humanize(r.status)}</Pill>;
                          },
                          csv: function v(r) {
                            return r.status;
                          },
                        },
                        {
                          id: "adGroup",
                          label: "Ad group",
                          sort: function s(r) {
                            return r.adGroup;
                          },
                          cell: function c(r) {
                            return r.adGroup;
                          },
                          csv: function v(r) {
                            return r.adGroup;
                          },
                        },
                        {
                          id: "campaign",
                          label: "Campaign",
                          sort: function s(r) {
                            return r.campaign;
                          },
                          cell: function c(r) {
                            return r.campaign;
                          },
                          csv: function v(r) {
                            return r.campaign;
                          },
                        },
                      ]}
                    />
                  </>
                ) : null}

                {health.lowQuality.length > 0 ? (
                  <>
                    <h4 className="ga-subhead">Keywords with a poor Quality Score</h4>
                    <DataTable
                      rows={health.lowQuality}
                      caption="Keywords with Quality Score 4 or below"
                      rowKey={function key(r) {
                        return r.id + "|" + r.adGroup;
                      }}
                      initialSort={{ id: "score", dir: "asc" }}
                      search={{
                        placeholder: "Search these keywords",
                        text: function text(r) {
                          return r.text + " " + r.campaign + " " + r.adGroup;
                        },
                      }}
                      pageSize={10}
                      exportName="google-ads-poor-quality-keywords"
                      columns={[
                        {
                          id: "text",
                          label: "Keyword",
                          sort: function s(r) {
                            return r.text;
                          },
                          cell: function c(r) {
                            return <span className="ga-strong">{r.text}</span>;
                          },
                          csv: function v(r) {
                            return r.text;
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
                            return <span className="ga-score is-bad">{r.score}</span>;
                          },
                          csv: function v(r) {
                            return r.score;
                          },
                        },
                        {
                          id: "campaign",
                          label: "Campaign",
                          sort: function s(r) {
                            return r.campaign;
                          },
                          cell: function c(r) {
                            return r.campaign;
                          },
                          csv: function v(r) {
                            return r.campaign;
                          },
                        },
                        {
                          id: "adGroup",
                          label: "Ad group",
                          sort: function s(r) {
                            return r.adGroup;
                          },
                          cell: function c(r) {
                            return r.adGroup;
                          },
                          csv: function v(r) {
                            return r.adGroup;
                          },
                        },
                      ]}
                    />
                  </>
                ) : null}

                {health.paused.length > 0 ? (
                  <>
                    <h4 className="ga-subhead">Paused campaigns</h4>
                    <ul className="ga-chips">
                      {health.paused.map(function chip(c) {
                        return (
                          <li key={c.id} className="ga-chip" title={c.name}>
                            {c.name}
                          </li>
                        );
                      })}
                    </ul>
                  </>
                ) : null}
              </>
            );
          }}
        </Slot>
      </Card>

      <Card
        title="Google's recommendations"
        note="Suggestions Google has open on this account, grouped by type. Apply or dismiss them in Google Ads."
      >
        <Slot slice={ctx.slice("recs")} onRetry={ctx.retry} height={160}>
          {function render(payload) {
            const recs = parseRecommendations(payload);
            if (recs === null || recs.total === 0) {
              return <Empty>{noteOf(payload) || "Google has no open recommendations for this account."}</Empty>;
            }
            const top = recs.types.length > 0 ? recs.types[0].count : 0;
            return (
              <>
                <p className="ga-foot-note is-top">
                  {fmtNum(recs.total, 0) + (recs.total === 1 ? " open recommendation" : " open recommendations") +
                    (recs.total >= 500 ? " (the first 500 are counted)" : "") + "."}
                </p>
                <ul className="ga-bars">
                  {recs.types.map(function row(r) {
                    return (
                      <li key={r.type} className="ga-bars-row">
                        <span className="ga-bars-label">{humanize(r.type)}</span>
                        <ShareBar value={r.count} max={top} tone="amber" />
                        <span className="ga-bars-num">{r.count}</span>
                      </li>
                    );
                  })}
                </ul>
              </>
            );
          }}
        </Slot>
      </Card>

      <Card title="Recent changes" note="Who changed what in the last 14 days." wide>
        <Slot slice={ctx.slice("changes")} onRetry={ctx.retry} height={240}>
          {function render(payload) {
            const events = parseChanges(payload);
            if (events.length === 0) {
              return <Empty>{noteOf(payload) || "No changes were recorded in the last 14 days."}</Empty>;
            }
            return (
              <DataTable
                rows={events}
                caption="Account change history"
                rowKey={function key(e) {
                  return e.when + "|" + e.user + "|" + e.resourceType + "|" + e.fields + "|" + e.campaign;
                }}
                initialSort={{ id: "when", dir: "desc" }}
                search={{
                  placeholder: "Search changes",
                  text: function text(e: ChangeEvent) {
                    return e.user + " " + e.resourceType + " " + e.fields + " " + e.campaign;
                  },
                }}
                exportName="google-ads-change-history"
                pageSize={15}
                columns={[
                  {
                    id: "when",
                    label: "When",
                    sort: function s(e) {
                      return e.when;
                    },
                    cell: function c(e) {
                      return <span className="ga-mono">{whenLabel(e.when)}</span>;
                    },
                    csv: function v(e) {
                      return e.when;
                    },
                  },
                  {
                    id: "user",
                    label: "Who",
                    sort: function s(e) {
                      return e.user;
                    },
                    cell: function c(e) {
                      return (
                        <span className="ga-cell-stack is-left">
                          <span title={e.user}>{e.user || "Unknown"}</span>
                          <span className="ga-muted ga-tiny">{humanize(e.clientType)}</span>
                        </span>
                      );
                    },
                    csv: function v(e) {
                      return e.user;
                    },
                  },
                  {
                    id: "what",
                    label: "What",
                    sort: function s(e) {
                      return e.resourceType;
                    },
                    cell: function c(e) {
                      return (
                        <span className="ga-cell-inline">
                          <Pill tone={operationTone(e.operation)}>{humanize(e.operation)}</Pill>
                          <span>{humanize(e.resourceType)}</span>
                        </span>
                      );
                    },
                    csv: function v(e) {
                      return e.operation + " " + e.resourceType;
                    },
                  },
                  {
                    id: "fields",
                    label: "Fields",
                    cell: function c(e) {
                      return <span className="ga-muted" title={e.fields}>{e.fields || "—"}</span>;
                    },
                    csv: function v(e) {
                      return e.fields;
                    },
                    className: "ga-col-wide",
                  },
                  {
                    id: "campaign",
                    label: "Campaign",
                    sort: function s(e) {
                      return e.campaign;
                    },
                    cell: function c(e) {
                      return <span className="ga-muted">{e.campaign || "—"}</span>;
                    },
                    csv: function v(e) {
                      return e.campaign;
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
