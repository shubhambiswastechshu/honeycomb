"use client";

/**
 * Decides which page frame the root layout puts around a route.
 *
 * The auth screens want the centred column plus the site footer. The dashboard
 * owns the full viewport and must not inherit either: the footer would leak in
 * below the rail, and .page-main's centring padding would fight the shell. So
 * under /dashboard this renders the children bare and returns no footer.
 *
 * `children` is passed through untouched, so the pages below stay server
 * components even though this wrapper is a client one.
 */

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import SiteFooter from "@/components/ui/SiteFooter";

function isDashboard(pathname: string): boolean {
  return pathname === "/dashboard" || pathname.indexOf("/dashboard/") === 0;
}

export default function AppChrome({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  if (isDashboard(pathname)) {
    return <>{children}</>;
  }

  return (
    <div className="page">
      <div className="page-main">{children}</div>
      <SiteFooter />
    </div>
  );
}
