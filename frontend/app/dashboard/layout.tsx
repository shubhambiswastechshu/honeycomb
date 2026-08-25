import type { Metadata } from "next";
import type { ReactNode } from "react";
import SessionProvider from "@/components/dashboard/SessionProvider";
import IconRail from "@/components/dashboard/IconRail";
import TopBar from "@/components/dashboard/TopBar";
import "./dashboard.css";

export const metadata: Metadata = {
  title: "Dashboard | Honeycomb",
};

/**
 * The dashboard shell. It is a server component and fetches nothing: the
 * identity is read client-side by SessionProvider, which holds the shell back
 * behind a loading screen until /auth/me/ answers. Route protection itself
 * happens earlier, in middleware.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <div className="dash">
        <TopBar />
        <div className="dash-body">
          <IconRail />
          <main className="dash-main">{children}</main>
        </div>
      </div>
    </SessionProvider>
  );
}
