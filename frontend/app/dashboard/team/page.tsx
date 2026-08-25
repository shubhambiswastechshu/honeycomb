import { Users } from "lucide-react";
import EmptyState from "@/components/dashboard/EmptyState";

export default function TeamPage() {
  return (
    <div className="panel">
      <h1 className="panel-title">Team</h1>
      <p className="panel-lede">People who can reach this workspace.</p>

      <div className="panel-body">
        <EmptyState
          icon={Users}
          title="No teammates yet"
          description="People invited to this workspace will be listed here."
        />
      </div>
    </div>
  );
}
