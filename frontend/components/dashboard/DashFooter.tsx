/**
 * The dashboard status bar: one quiet fixed row along the bottom of the shell.
 *
 * Not SiteFooter. That one is a marketing footer for the auth screens -- it
 * links to /signin and /signup, which are exactly the two places a signed-in
 * user has no reason to go, and it is sized to sit at the end of a scrolling
 * page rather than to hold a fixed edge.
 *
 * Deliberately a server component and deliberately empty of navigation: the
 * rail already carries every dashboard route, so links here would be a second
 * copy to keep in sync for no gain. The year is resolved on the server, once,
 * so there is nothing for hydration to disagree about.
 */

export default function DashFooter() {
  return (
    <footer className="dash-footer">
      <p className="dash-footer-legal">
        &copy; {new Date().getFullYear()} Honeycomb
      </p>
    </footer>
  );
}
