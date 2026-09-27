"use client";

/**
 * Reports: every dashboard this workspace has built from its connections' data.
 *
 * "New report" creates an empty one immediately and opens it -- there is no
 * form to fill in first. A marketer names it, picks a client and adds widgets
 * once they are looking at the (empty) canvas, the same way a blank document
 * works, not the same way a wizard does.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { BarChart3 } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import EmptyState from "@/components/dashboard/EmptyState";
import PanelCover from "@/components/dashboard/PanelCover";
import SavedReportsList from "@/components/dashboard/SavedReportsList";
import { createSavedReport, deleteSavedReport, listSavedReports } from "@/lib/api";
import type { SavedReportSummary } from "@/lib/api";

const LOAD_ERROR = "Could not load your reports.";

function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message.length > 0 ? caught.message : fallback;
}

export default function ReportsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<SavedReportSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<boolean>(false);
  const [pendingDelete, setPendingDelete] = useState<SavedReportSummary | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const aliveRef = useRef(true);
  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const load = useCallback(async function load(): Promise<void> {
    try {
      const next = await listSavedReports();
      if (aliveRef.current) {
        setRows(next);
        setError(null);
      }
    } catch (caught) {
      if (aliveRef.current) setError(messageOf(caught, LOAD_ERROR));
    }
  }, []);

  useEffect(
    function loadOnMount() {
      void load();
    },
    [load]
  );

  async function handleCreate(): Promise<void> {
    if (creating) return;
    setCreating(true);
    try {
      const report = await createSavedReport();
      router.push("/dashboard/reports/" + report.id);
    } catch (caught) {
      if (aliveRef.current) {
        setError(messageOf(caught, "That report could not be created."));
        setCreating(false);
      }
    }
  }

  async function handleDelete(): Promise<void> {
    const target = pendingDelete;
    if (target === null || deleting) return;
    setDeleting(true);
    try {
      await deleteSavedReport(target.id);
      setRows(function without(current) {
        return current === null ? current : current.filter((row) => row.id !== target.id);
      });
      setDeleteError(null);
      setPendingDelete(null);
    } catch (caught) {
      setDeleteError(messageOf(caught, "That report could not be deleted."));
    } finally {
      if (aliveRef.current) setDeleting(false);
    }
  }

  return (
    <div className="panel panel-wide">
      <PanelCover
        title="Reports"
        lede="Dashboards your team builds from your connections' data, grouped by client."
      />

      <div className="panel-body">
        {error !== null ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : rows === null ? (
          <p className="conn-loading">Loading&hellip;</p>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title="No reports yet"
            description="Build a dashboard from your connections' data: pick a connection, pick what to show, and it's live. Group reports by client as you make them."
            action={
              <button type="button" className="conn-action conn-action-primary" onClick={handleCreate} disabled={creating}>
                {creating ? "Creating…" : "New report"}
              </button>
            }
          />
        ) : (
          <>
            <div className="panel-head rpt-head">
              <h2 className="rpt-heading">All reports</h2>
              <div className="panel-head-actions">
                <button
                  type="button"
                  className="conn-action conn-action-primary"
                  onClick={handleCreate}
                  disabled={creating}
                >
                  {creating ? "Creating…" : "New report"}
                </button>
              </div>
            </div>
            <SavedReportsList
              rows={rows}
              onDelete={function onDelete(row) {
                setDeleteError(null);
                setPendingDelete(row);
              }}
            />
          </>
        )}
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this report?"
        description={
          <>
            <strong>{pendingDelete?.name}</strong> will be gone for everyone in the workspace. This
            cannot be undone.
            {deleteError !== null ? (
              <p className="error" role="alert">
                {deleteError}
              </p>
            ) : null}
          </>
        }
        confirmLabel="Delete report"
        pendingLabel="Deleting…"
        destructive
        pending={deleting}
        onConfirm={handleDelete}
        onCancel={function onCancel() {
          setPendingDelete(null);
          setDeleteError(null);
        }}
      />
    </div>
  );
}
