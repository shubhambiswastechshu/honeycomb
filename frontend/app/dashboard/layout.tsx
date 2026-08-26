import type { Metadata } from "next";
import type { ReactNode } from "react";
import SessionProvider from "@/components/dashboard/SessionProvider";
import IconRail from "@/components/dashboard/IconRail";
import TopBar from "@/components/dashboard/TopBar";
import DashFooter from "@/components/dashboard/DashFooter";
import "./dashboard.css";
// After dashboard.css: the consoles take over the shell's main pane, and
// .dash-main:has(> .ide) has to win over the padding set there.
import "./ide.css";

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
        <DashFooter />
      </div>
    </SessionProvider>
  );
}
