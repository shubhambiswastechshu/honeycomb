import { Activity } from "lucide-react";
import EmptyState from "@/components/dashboard/EmptyState";

export default function ActivityPage() {
  return (
    <div className="panel">
      <h1 className="panel-title">Activity</h1>
      <p className="panel-lede">What has been happening in this workspace.</p>

      <div className="panel-body">
        <EmptyState
          icon={Activity}
          title="No activity yet"
          description="Changes made in this workspace will show up here as they happen."
        />
      </div>
    </div>
  );
}
