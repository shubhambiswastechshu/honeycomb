import type { Metadata } from "next";
import type { ReactNode } from "react";
import SessionProvider from "@/components/dashboard/SessionProvider";
import LiveProvider from "@/components/dashboard/LiveProvider";
import LivePanel from "@/components/dashboard/LivePanel";
import IconRail from "@/components/dashboard/IconRail";
import TopBar from "@/components/dashboard/TopBar";
import DashFooter from "@/components/dashboard/DashFooter";
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
      {/* Inside the session: nothing here fetches until the identity has
          arrived, and signing out unmounts it and stops every timer. */}
      <LiveProvider>
        <div className="dash">
          <TopBar />
          <div className="dash-body">
            <IconRail />
            <main className="dash-main">{children}</main>
            {/* Docked beside the page on wide screens, over it on narrow
                ones, and absent while closed. */}
            <LivePanel />
          </div>
          <DashFooter />
        </div>
      </LiveProvider>
    </SessionProvider>
  );
}
