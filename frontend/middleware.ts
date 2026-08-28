import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * URL-level route guard.
 *
 * IMPORTANT, and deliberately so: this middleware only checks whether an auth
 * cookie is PRESENT. It does not verify the JWT signature, does not check its
 * expiry and does not talk to the backend. Django is the real authority --
 * every API call is authenticated and authorised server-side, and a forged or
 * expired cookie gets nothing from it. The guard exists to stop the flash of
 * protected chrome (and the pointless round trip) when a signed out visitor
 * types a /dashboard URL. It is not the security boundary.
 *
 * It checks for EITHER cookie, and the "either" matters. An access token lasts
 * an hour and a session lasts weeks, so there is a normal, expected state
 * where the access cookie is stale or gone and the refresh cookie is perfectly
 * good. Redirecting on the access cookie alone signed people out an hour after
 * they arrived: this runs at the edge, so it fired before any page script
 * could do the 401-then-refresh dance that would have renewed them silently.
 * If both are gone there is nothing left to renew from, and sign-in is right.
 *
 * Middleware runs on the server, which is why it can read an httpOnly cookie
 * at all -- client JavaScript cannot, and that is the point of keeping the
 * tokens out of browser storage.
 */

const ACCESS_COOKIE = "hc_access";
const REFRESH_COOKIE = "hc_refresh";
const SIGN_IN_PATH = "/signin";
const SIGN_UP_PATH = "/signup";
const DASHBOARD_PATH = "/dashboard";

const REDIRECT_STATUS = 307;

/** Applied to every response, redirects included. */
function harden(response: NextResponse): NextResponse {
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  return response;
}

function isDashboardPath(pathname: string): boolean {
  return (
    pathname === DASHBOARD_PATH || pathname.indexOf(DASHBOARD_PATH + "/") === 0
  );
}

export function middleware(request: NextRequest): NextResponse {
  const pathname = request.nextUrl.pathname;
  const signedIn =
    request.cookies.get(ACCESS_COOKIE) !== undefined ||
    request.cookies.get(REFRESH_COOKIE) !== undefined;

  // Protected area: bounce to sign in and remember where they were headed.
  if (isDashboardPath(pathname) && !signedIn) {
    const url = request.nextUrl.clone();
    url.pathname = SIGN_IN_PATH;
    url.search = "";
    url.searchParams.set("next", pathname);
    return harden(NextResponse.redirect(url, REDIRECT_STATUS));
  }

  // Auth pages are pointless for someone who already has a session -- unless
  // they arrived with an explicit ?next=, which only happens when something
  // downstream decided the session is unusable and asked for the sign-in form.
  // Bouncing that back to /dashboard on cookie presence alone would trade
  // redirects with it forever, so an explicit ?next= always wins.
  const isAuthPage = pathname === SIGN_IN_PATH || pathname === SIGN_UP_PATH;
  if (isAuthPage && signedIn && !request.nextUrl.searchParams.has("next")) {
    const url = request.nextUrl.clone();
    url.pathname = DASHBOARD_PATH;
    url.search = "";
    return harden(NextResponse.redirect(url, REDIRECT_STATUS));
  }

  // The root is a switchboard, never a page.
  if (pathname === "/") {
    const url = request.nextUrl.clone();
    url.pathname = signedIn ? DASHBOARD_PATH : SIGN_IN_PATH;
    url.search = "";
    return harden(NextResponse.redirect(url, REDIRECT_STATUS));
  }

  return harden(NextResponse.next());
}

// "/dashboard" is listed alongside "/dashboard/:path*" so the bare index route
// is matched whatever the path matcher does with a zero-segment wildcard.
export const config = {
  matcher: ["/", "/signin", "/signup", "/dashboard", "/dashboard/:path*"],
};
