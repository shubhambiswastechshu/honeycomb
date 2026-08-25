import type { Metadata } from "next";
import type { ReactNode } from "react";
import AppChrome from "@/components/dashboard/AppChrome";
import "./globals.css";

export const metadata: Metadata = {
  title: "Honeycomb",
  description: "Honeycomb authentication",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* AppChrome adds the centred column + site footer outside /dashboard,
            and steps aside for the dashboard's own full-viewport shell. */}
        <AppChrome>{children}</AppChrome>
      </body>
    </html>
  );
}
