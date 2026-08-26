"use client";

/**
 * "Test" on a data source row: open a real connection and say what happened.
 *
 * The result is deliberately shown next to the button rather than as a toast.
 * A failure here is usually a sentence someone has to act on -- a missing
 * password, a host that does not resolve, a role without CONNECT -- and a
 * message that disappears after four seconds is a message nobody can copy into
 * a ticket.
 *
 * `refresh` is called on the way out because the server writes the outcome to
 * the row, so the status shown in the list would otherwise disagree with the
 * result shown beside it.
 */

import { useState } from "react";
import { AlertTriangle, Check, Loader2, PlugZap } from "lucide-react";
import { testDataSource } from "@/lib/api";
import type { DataSource } from "@/lib/api";

export interface TestSourceButtonProps {
  source: DataSource;
  refresh: () => Promise<void>;
}

export default function TestSourceButton({ source, refresh }: TestSourceButtonProps) {
  const [testing, setTesting] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  async function test(): Promise<void> {
    setTesting(true);
    setOutcome(null);
    try {
      const updated = await testDataSource(source.id);
      setOutcome({
        ok: updated.status === "CONNECTED",
        text: updated.detail || updated.last_error || "No answer.",
      });
      await refresh();
    } catch (caught) {
      setOutcome({
        ok: false,
        text: caught instanceof Error ? caught.message : "The test could not run.",
      });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="source-test">
      <button
        type="button"
        className="source-test-button"
        onClick={function go() {
          void test();
        }}
        disabled={testing}
        title="Open a connection and report what answered"
      >
        {testing ? (
          <Loader2 size={13} className="ide-spin" aria-hidden="true" />
        ) : (
          <PlugZap size={13} strokeWidth={1.75} aria-hidden="true" />
        )}
        {testing ? "Testing" : "Test"}
      </button>

      {outcome !== null ? (
        <p
          className={outcome.ok ? "source-test-note source-test-ok" : "source-test-note"}
          role="status"
        >
          {outcome.ok ? (
            <Check size={12} strokeWidth={2.5} aria-hidden="true" />
          ) : (
            <AlertTriangle size={12} strokeWidth={2} aria-hidden="true" />
          )}
          {outcome.text}
        </p>
      ) : null}
    </div>
  );
}
