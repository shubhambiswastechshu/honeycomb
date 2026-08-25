import Link from "next/link";
import { LogoMark } from "@/components/ui/Logo";

/** Full-width page footer: one quiet row, rendered once by the root layout. */
export default function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="site-footer-inner">
        <div className="site-footer-name">
          <LogoMark size={16} />
          <span>Honeycomb</span>
        </div>

        <nav className="site-footer-nav" aria-label="Footer">
          <Link href="/signin">Sign in</Link>
          <Link href="/signup">Create workspace</Link>
        </nav>

        <p className="site-footer-legal">
          &copy; {new Date().getFullYear()} Honeycomb
        </p>
      </div>
    </footer>
  );
}
