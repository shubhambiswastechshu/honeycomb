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
    return "http://localhost:8000";
  }
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
    ? "connect-src 'self' " + apiOrigin
    : "connect-src 'self' " + apiOrigin + " ws: wss:",
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
};

export default nextConfig;
