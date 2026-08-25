import { Database } from "lucide-react";
import EmptyState from "@/components/dashboard/EmptyState";

export default function DataPage() {
  return (
    <div className="panel">
      <h1 className="panel-title">Data</h1>
      <p className="panel-lede">Everything this workspace stores.</p>

      <div className="panel-body">
        <EmptyState
          icon={Database}
          title="No data yet"
          description="Records you add to this workspace will be listed here."
        />
      </div>
    </div>
  );
}
