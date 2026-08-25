const isProduction = process.env.NODE_ENV === "production";

/**
 * Where the API lives, so connect-src can name it instead of opening up to
 * anything. Kept in step with lib/api.ts, which reads the same variable.
 */
const apiOrigin = (function readApiOrigin() {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api";
  try {
    return new URL(raw).origin;
  } catch (invalid) {
    // A relative base ("/api") is not a URL and lands here. That is the
    // deployed shape: the browser calls this origin and the rewrite below
    // forwards to Django, so 'self' in connect-src already covers it.
    return "";
  }
})();

/**
 * Where the rewrite forwards /api/* in a deployment.
 *
 * Render exposes the API service as "host:port" via fromService, with no
 * scheme, so one is added. Unset locally, where the browser talks to :8000
 * directly and no rewrite is registered.
 */
const apiProxyTarget = (function readProxyTarget() {
  const raw = (process.env.API_ORIGIN || "").trim().replace(/\/+$/, "");
  if (raw === "") {
    return null;
  }
  return /^https?:\/\//.test(raw) ? raw : "https://" + raw;
})();

/**
 * Content-Security-Policy.
 *
 * script-src carries 'unsafe-inline' because the App Router bootstraps its
 * payload through inline <script> tags; removing it means generating a nonce
 * per request in middleware.ts and threading it through, which is a larger
 * change than this one. Everything else is locked down: no plugins, no
 * framing, no <base> rewriting, no form posts off-origin, and connect-src
 * limited to this origin plus the API. 'unsafe-eval' and the HMR websocket are
 * dev-only -- the production build needs neither.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  isProduction
    ? "script-src 'self' 'unsafe-inline'"
    : "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  // React style props and Next's injected <style> blocks are inline styles.
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  isProduction
    ? ("connect-src 'self' " + apiOrigin).trim()
    : ("connect-src 'self' " + apiOrigin + " ws: wss:").replace(/\s+/g, " "),
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "frame-src 'none'",
].join("; ");

/**
 * Applied to every path. middleware.ts sets three of these too, but its matcher
 * only covers five routes -- these cover everything the app serves.
 */
const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=()",
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  // `next build` and `next dev` must not share a directory: a production build
  // run while the dev server is up replaces the chunk manifest under it, after
  // which the dev server keeps answering 200/307 for every route while every
  // client chunk 500s ("Cannot find module './948.js'"). Splitting the output
  // makes the two independent. NODE_ENV is "production" for `next build` and
  // `next start` alike, so both agree on .next-build.
  distDir: isProduction ? ".next-build" : ".next",

  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },

  /**
   * Same-origin proxy to Django.
   *
   * Every *.onrender.com subdomain is a separate site to a browser, because
   * onrender.com sits on the Public Suffix List. Calling the API on its own
   * subdomain would therefore be cross-site, and the SameSite=Lax auth cookies
   * would never be sent -- sign-in would appear to work and every later request
   * would 401. Routing /api/* through this server keeps the whole session
   * same-origin, so Lax stays and CORS stops mattering.
   *
   * Registered only when API_ORIGIN is set. Locally it is not, and the browser
   * talks to :8000 directly, which is same-site because both are localhost.
   */
  async rewrites() {
    if (apiProxyTarget === null) {
      return [];
    }
    return [
      {
        source: "/api/:path*",
        destination: apiProxyTarget + "/api/:path*",
      },
    ];
  },
};

export default nextConfig;
