"use client";

import { LayoutGrid } from "lucide-react";
import EmptyState from "@/components/dashboard/EmptyState";
import { useSession } from "@/components/dashboard/SessionProvider";

/**
 * Overview. The only panel that shows real values, and both of them come
 * straight from the session: the user's full name and their organization.
 */
export default function OverviewPage() {
  const { session } = useSession();

  return (
    <div className="panel">
      <h1 className="panel-title">Welcome, {session.user.full_name}</h1>
      <p className="panel-lede">
        You are signed in to {session.tenant.name}.
      </p>

      <div className="panel-body">
        <EmptyState
          icon={LayoutGrid}
          title="Nothing to show yet"
          description="This workspace is new, so there is nothing on the overview so far."
        />
      </div>
    </div>
  );
}
