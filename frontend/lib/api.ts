/**
 * Tiny typed client for the Honeycomb API.
 *
 * Auth is cookie based. The backend sets "hc_access" and "hc_refresh" as
 * httpOnly cookies, so this file never sees, stores or forwards a token --
 * there is no Authorization header and no browser storage. Every request
 * goes out with credentials: "include" and every unsafe method carries the
 * "X-CSRFToken" header read from the (deliberately readable) csrftoken cookie,
 * because cookie authentication without a CSRF check is a CSRF hole.
 *
 * Contract:
 *   GET   /auth/csrf/             -> {ok}            sets csrftoken
 *   POST  /auth/signup/           -> {user, tenant}  sets auth cookies
 *   POST  /auth/signin/           -> {user, tenant}  sets auth cookies
 *   POST  /auth/signup/check/     -> {ok} | 400 field map
 *   POST  /auth/refresh/          -> {ok}            reads hc_refresh cookie
 *   POST  /auth/logout/           -> {ok}            clears auth cookies
 *   GET   /auth/me/               -> {user, tenant}
 *   PATCH /auth/me/               -> {user, tenant}
 *   POST  /auth/change-email/     -> {user, tenant}
 *   POST  /auth/change-password/  -> {ok}            reissues auth cookies
 *   PATCH /tenant/                -> tenant
 *
 * Sign in may answer 409 with {"detail", "organizations": [...]} when the
 * address is valid in several organizations; see AmbiguousOrganizationError.
 */

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: string;
}

export interface Tenant {
  id: number;
  name: string;
  slug: string;
}

/** What the identity endpoints return, and what pages keep in component state. */
export interface Session {
  user: User;
  tenant: Tenant;
}

export interface SignUpPayload {
  organization_name: string;
  full_name: string;
  email: string;
  password: string;
}

export interface SignInPayload {
  email: string;
  password: string;
  /**
   * Only needed when the address exists in more than one organization: the
   * server answers 409 with the choices and the client re-POSTs with one.
   */
  organization_slug?: string;
}

export interface UpdateProfilePayload {
  full_name: string;
}

export interface ChangeEmailPayload {
  new_email: string;
  current_password: string;
}

export interface ChangePasswordPayload {
  current_password: string;
  new_password: string;
}

export interface UpdateTenantPayload {
  name: string;
}

/** One entry of the 409 "organizations" list returned by POST /auth/signin/. */
export interface OrganizationChoice {
  id: number;
  name: string;
  slug: string;
}

/**
 * Thrown for the HTTP 409 that sign in returns when the credentials are valid
 * in several organizations. Carries the list the caller must pick from.
 */
export class AmbiguousOrganizationError extends Error {
  organizations: OrganizationChoice[];

  constructor(message: string, organizations: OrganizationChoice[]) {
    super(message);
    this.name = "AmbiguousOrganizationError";
    this.organizations = organizations;
    // Keeps `instanceof` working when the class is transpiled.
    Object.setPrototypeOf(this, AmbiguousOrganizationError.prototype);
  }
}

/** Internal: an ordinary Error that also remembers the HTTP status. */
class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    Object.setPrototypeOf(this, ApiError.prototype);
  }
}

const RAW_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api";

/** Base URL with any trailing slashes removed, so BASE + "/auth/me/" is always well formed. */
export const API_BASE: string = RAW_BASE.replace(/\/+$/, "");

const CSRF_COOKIE = "csrftoken";
const CSRF_HEADER = "X-CSRFToken";

const GENERIC_ERROR =
  "Something went wrong. Is the backend running on port 8000?";

/**
 * Pull a human-readable message out of whatever the server sent back.
 * Handles {"detail": "..."}, {"email": ["..."]}, {"email": "..."} and
 * non-JSON bodies (an HTML 500 page, an empty body, a proxy error).
 */
function extractErrorMessage(body: unknown, status: number): string {
  if (typeof body === "string") {
    const trimmed = body.trim();
    // An HTML error page or a wall of text is noise, not a message.
    if (trimmed.length > 0 && trimmed.length <= 200 && trimmed.charAt(0) !== "<") {
      return trimmed;
    }
    return GENERIC_ERROR;
  }

  if (body !== null && typeof body === "object") {
    const record = body as Record<string, unknown>;

    const detail = record["detail"];
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }

    // First field error wins: {"email": ["A user with this email already exists."]}
    // The key is kept so a multi-field 400 says *which* field is wrong.
    const keys = Object.keys(record);
    for (let index = 0; index < keys.length; index += 1) {
      const key = keys[index];
      const value = record[key];
      let message: string | null = null;
      if (Array.isArray(value) && value.length > 0 && typeof value[0] === "string") {
        message = value[0];
      } else if (typeof value === "string" && value.length > 0) {
        message = value;
      }
      if (message !== null) {
        if (key === "detail" || key === "non_field_errors") {
          return message;
        }
        return key.replace(/_/g, " ") + ": " + message;
      }
    }
  }

  if (status === 401) {
    return "Invalid email or password.";
  }
  return GENERIC_ERROR;
}

/** Read the 409 sign-in body's "organizations" list, ignoring malformed entries. */
function parseOrganizations(body: unknown): OrganizationChoice[] | null {
  if (body === null || typeof body !== "object") {
    return null;
  }
  const raw = (body as Record<string, unknown>)["organizations"];
  if (!Array.isArray(raw)) {
    return null;
  }
  const choices: OrganizationChoice[] = [];
  for (let index = 0; index < raw.length; index += 1) {
    const entry: unknown = raw[index];
    if (entry !== null && typeof entry === "object") {
      const record = entry as Record<string, unknown>;
      const id = record["id"];
      const name = record["name"];
      const slug = record["slug"];
      if (
        typeof id === "number" &&
        typeof name === "string" &&
        typeof slug === "string"
      ) {
        choices.push({ id: id, name: name, slug: slug });
      }
    }
  }
  return choices.length > 0 ? choices : null;
}

/** document.cookie is unavailable while rendering on the server. */
function readCookie(name: string): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const parts = document.cookie.split(";");
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index].trim();
    if (part.indexOf(name + "=") === 0) {
      return decodeURIComponent(part.slice(name.length + 1));
    }
  }
  return null;
}

function isUnsafeMethod(method: string): boolean {
  const upper = method.toUpperCase();
  return (
    upper === "POST" ||
    upper === "PATCH" ||
    upper === "PUT" ||
    upper === "DELETE"
  );
}

interface RequestConfig {
  method: string;
  /** Serialized as JSON when present; omitted entirely otherwise. */
  body?: Record<string, unknown>;
  /**
   * True for endpoints that require an authenticated user, where a 401 means
   * "the access cookie expired" rather than "these credentials are wrong".
   * Only those retry through /auth/refresh/.
   */
  authenticated?: boolean;
}

/** One round trip. No CSRF priming, no refresh handling -- see request(). */
async function send<T>(path: string, config: RequestConfig): Promise<T> {
  const headers: Record<string, string> = {};
  if (config.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (isUnsafeMethod(config.method)) {
    const token = readCookie(CSRF_COOKIE);
    if (token !== null) {
      headers[CSRF_HEADER] = token;
    }
  }

  let response: Response;
  try {
    response = await fetch(API_BASE + path, {
      method: config.method,
      headers: headers,
      // The auth cookies are httpOnly, so they only travel if we ask for them.
      credentials: "include",
      body: config.body !== undefined ? JSON.stringify(config.body) : undefined,
    });
  } catch (networkError) {
    // fetch only rejects on network-level failures (backend down, CORS blocked).
    throw new ApiError(GENERIC_ERROR, 0);
  }

  const raw = await response.text();
  let parsed: unknown = null;
  if (raw.length > 0) {
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch (parseError) {
      // Not JSON (HTML error page, plain text). Keep the raw body for extraction.
      parsed = raw;
    }
  }

  if (!response.ok) {
    const message = extractErrorMessage(parsed, response.status);
    if (response.status === 409) {
      const organizations = parseOrganizations(parsed);
      if (organizations !== null) {
        throw new AmbiguousOrganizationError(message, organizations);
      }
    }
    throw new ApiError(message, response.status);
  }

  return parsed as T;
}

/* ------------------------------------------------------------------ */
/* CSRF priming                                                        */
/* ------------------------------------------------------------------ */

let csrfPromise: Promise<void> | null = null;

/**
 * Make sure a csrftoken cookie exists before the first unsafe request.
 * Memoised: the GET happens once per page load, not once per call. A failure
 * clears the memo so the next attempt can try again.
 */
export function ensureCsrf(): Promise<void> {
  if (csrfPromise === null) {
    if (readCookie(CSRF_COOKIE) !== null) {
      csrfPromise = Promise.resolve();
    } else {
      csrfPromise = send<{ ok: boolean }>("/auth/csrf/", { method: "GET" })
        .then(function discardBody() {
          return undefined;
        })
        .catch(function forget(error: unknown) {
          csrfPromise = null;
          throw error;
        });
    }
  }
  return csrfPromise;
}

/**
 * Django rotates the CSRF token when the session identity changes, and the
 * cookie is gone once the server clears it. Dropping the memo makes the next
 * unsafe request re-prime instead of sending a stale header.
 */
function forgetCsrf(): void {
  csrfPromise = null;
}

/* ------------------------------------------------------------------ */
/* Refresh                                                             */
/* ------------------------------------------------------------------ */

let refreshInFlight: Promise<boolean> | null = null;

/**
 * Spend the hc_refresh cookie for a fresh hc_access cookie. Resolves true when
 * the session was renewed and false when it is gone for good -- it never
 * rejects, so callers can branch on the result.
 *
 * A burst of 401s (a dashboard mounting four panels at once) shares a single
 * in-flight POST rather than firing four refreshes and rotating the token from
 * under each other. The call itself is never marked `authenticated`, so a 401
 * here can never trigger another refresh: recursion is structurally impossible.
 */
function refreshSession(): Promise<boolean> {
  if (refreshInFlight === null) {
    refreshInFlight = ensureCsrf()
      .then(function postRefresh() {
        return send<{ ok: boolean }>("/auth/refresh/", { method: "POST" });
      })
      .then(function renewed() {
        return true;
      })
      .catch(function expired() {
        // Nothing to clear on this side -- the tokens were never ours to hold.
        // Drop the CSRF memo so the sign-in that follows primes a fresh token.
        forgetCsrf();
        return false;
      })
      .then(function settle(result: boolean) {
        refreshInFlight = null;
        return result;
      });
  }
  return refreshInFlight;
}

/**
 * Prime CSRF when needed, send the request, and on a 401 from an authenticated
 * call refresh once and replay the original request exactly once. The replay
 * carries no retry of its own, so a second 401 surfaces to the caller.
 */
async function request<T>(path: string, config: RequestConfig): Promise<T> {
  if (isUnsafeMethod(config.method)) {
    await ensureCsrf();
  }

  try {
    return await send<T>(path, config);
  } catch (caught) {
    const canRetry =
      config.authenticated === true &&
      caught instanceof ApiError &&
      caught.status === 401;
    if (!canRetry) {
      throw caught;
    }
    const renewed = await refreshSession();
    if (!renewed) {
      throw caught;
    }
    return await send<T>(path, config);
  }
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                           */
/* ------------------------------------------------------------------ */

/**
 * Ask the server to validate part of a signup in progress. Send only the
 * fields the current step owns; add context_email / context_full_name when
 * checking a password so the similarity rule has something to compare to.
 */
export interface SignUpCheckPayload {
  organization_name?: string;
  full_name?: string;
  email?: string;
  password?: string;
  context_email?: string;
  context_full_name?: string;
}

const FIELD_PREFIXES = [
  "organization name: ",
  "full name: ",
  "email: ",
  "password: ",
];

function stripFieldPrefix(message: string): string {
  const lowered = message.toLowerCase();
  for (let index = 0; index < FIELD_PREFIXES.length; index += 1) {
    const prefix = FIELD_PREFIXES[index];
    if (lowered.indexOf(prefix) === 0) {
      return message.slice(prefix.length);
    }
  }
  return message;
}

/**
 * Resolves when the step is acceptable and rejects with the server's message
 * otherwise. Throws the bare field message ("A user with this email already
 * exists.") rather than the "email: ..." form, because the wizard shows one
 * field at a time and the prefix would just repeat the visible label.
 */
export async function checkSignUpStep(
  payload: SignUpCheckPayload
): Promise<void> {
  try {
    await request<{ ok: boolean }>("/auth/signup/check/", {
      method: "POST",
      body: payload as Record<string, unknown>,
    });
  } catch (caught) {
    if (caught instanceof Error) {
      throw new Error(stripFieldPrefix(caught.message));
    }
    throw caught;
  }
}

/** Creates the organization and its first user. Auth cookies arrive in the response. */
export async function signUp(payload: SignUpPayload): Promise<Session> {
  const session = await request<Session>("/auth/signup/", {
    method: "POST",
    body: {
      organization_name: payload.organization_name,
      full_name: payload.full_name,
      email: payload.email,
      password: payload.password,
    },
  });
  // The server rotated the CSRF token along with the new identity.
  forgetCsrf();
  return session;
}

/** Auth cookies arrive in the response; nothing about them is readable here. */
export async function signIn(payload: SignInPayload): Promise<Session> {
  const body: Record<string, unknown> = {
    email: payload.email,
    password: payload.password,
  };
  // Omitted entirely unless the user has picked an organization, so the
  // ordinary request body stays exactly {email, password}.
  if (
    typeof payload.organization_slug === "string" &&
    payload.organization_slug.length > 0
  ) {
    body["organization_slug"] = payload.organization_slug;
  }
  const session = await request<Session>("/auth/signin/", {
    method: "POST",
    body: body,
  });
  forgetCsrf();
  return session;
}

/** Asks the server to delete both auth cookies. Rejects if it could not. */
export async function signOut(): Promise<void> {
  try {
    await request<{ ok: boolean }>("/auth/logout/", { method: "POST" });
  } finally {
    forgetCsrf();
  }
}

/** The current identity, or a rejection the caller turns into a redirect. */
export function me(): Promise<Session> {
  return request<Session>("/auth/me/", {
    method: "GET",
    authenticated: true,
  });
}

export function updateProfile(payload: UpdateProfilePayload): Promise<Session> {
  return request<Session>("/auth/me/", {
    method: "PATCH",
    body: { full_name: payload.full_name },
    authenticated: true,
  });
}

export function changeEmail(payload: ChangeEmailPayload): Promise<Session> {
  return request<Session>("/auth/change-email/", {
    method: "POST",
    body: {
      new_email: payload.new_email,
      current_password: payload.current_password,
    },
    authenticated: true,
  });
}

/** The server reissues both auth cookies, so the session survives the change. */
export async function changePassword(
  payload: ChangePasswordPayload
): Promise<void> {
  await request<{ ok: boolean }>("/auth/change-password/", {
    method: "POST",
    body: {
      current_password: payload.current_password,
      new_password: payload.new_password,
    },
    authenticated: true,
  });
  forgetCsrf();
}

/** Renames the organization. Rejects with 403 for anyone below ADMIN. */
export function updateTenant(payload: UpdateTenantPayload): Promise<Tenant> {
  return request<Tenant>("/tenant/", {
    method: "PATCH",
    body: { name: payload.name },
    authenticated: true,
  });
}

/* ================================================================== */
/* Connectors                                                          */
/*                                                                     */
/* The control plane for the MCP portal. Everything below is           */
/* tenant-scoped by the server: not one of these calls sends a tenant  */
/* id, because the backend reads it from request.user and would ignore */
/* the field anyway. A client that could name a tenant would be a      */
/* cross-tenant read waiting to happen, so the shape of this section   */
/* is deliberate -- there is no parameter here to get wrong.           */
/*                                                                     */
/* Every one of these endpoints requires a signed-in user, so they all */
/* pass authenticated: true and inherit request()'s single refresh and */
/* replay. Without it a dashboard whose hc_access cookie expired while */
/* the tab sat open would show an error instead of quietly renewing.   */
/*                                                                     */
/* Contract:                                                           */
/*   GET    /connectors/                     -> ConnectorSpec[]        */
/*   GET    /connectors/<slug>/              -> ConnectorDetail        */
/*   GET    /connections/?connector=<slug>   -> Connection[]           */
/*   POST   /connections/                    -> Connection             */
/*   GET    /connections/<id>/               -> Connection             */
/*   PATCH  /connections/<id>/               -> Connection             */
/*   DELETE /connections/<id>/               -> 204                    */
/*   GET    /connections/<id>/tools/         -> ConnectorTool[]        */
/*   POST   /connections/<id>/tools/         -> ConnectorTool[]        */
/*   GET    /connections/<id>/keys/          -> McpKeyRow[]            */
/*   POST   /connections/<id>/keys/          -> McpKeyRow & {token}    */
/*   DELETE /connections/<id>/keys/<key_id>/ -> 204                    */
/*   GET    /connections/<id>/activity/      -> ActivityRow[]          */
/* ================================================================== */

/**
 * One entry in the marketplace: a connector the server knows how to speak,
 * whether or not this tenant has connected it.
 *
 * connected_count is the number of connections this tenant already has for the
 * connector, and it is the only tenant-specific value on the type. The
 * marketplace leans on it for the honest card state -- "Connect" at zero,
 * "N connected" above it -- which is also why a card must never render a count
 * before this list has arrived. Zero and "not loaded yet" are different facts.
 *
 * cred_fields names the credentials a connect form has to collect. It is the
 * connector's own list, so the form stays data-driven and a newly registered
 * connector becomes connectable without a frontend change.
 */
export interface ConnectorSpec {
  slug: string;
  label: string;
  auth: string;
  description: string;
  category: string;
  cred_fields: string[];
  tool_count: number;
  connected_count: number;
}

/**
 * One tool a connector exposes over MCP.
 *
 * write is the connector author's declaration that the tool changes remote
 * state; the UI uses it to warn, not to block. enabled is present only when the
 * tool is read through a connection (GET/POST /connections/<id>/tools/),
 * because enablement belongs to the connection rather than the connector -- the
 * catalogue listing has no per-tenant answer for it, so the field is optional
 * instead of defaulting to a value that would be a guess.
 */
export interface ConnectorTool {
  name: string;
  description: string;
  write: boolean;
  enabled?: boolean;
}

/** A connector plus its full tool list, from GET /connectors/<slug>/. */
export interface ConnectorDetail extends ConnectorSpec {
  tools: ConnectorTool[];
}

/**
 * One configured instance of a connector for this tenant.
 *
 * Credentials are write-only by design and never appear here: they are
 * encrypted at rest and the serializer has no outbound field for them. If you
 * find yourself wanting a creds property, the answer is no -- send new values
 * through updateConnection() rather than reading the old ones back.
 *
 * endpoint_slug is generated once, at creation, and is never regenerated: it is
 * embedded in mcp_url, which people paste into MCP clients. Treat both as
 * stable identifiers, not as display detail that can be refreshed.
 */
export interface Connection {
  id: number;
  connector: string;
  connector_label: string;
  name: string;
  status: string;
  last_error: string;
  endpoint_slug: string;
  mcp_url: string;
  disabled_tools: string[];
  tool_count: number;
  key_count: number;
  created_at: string;
  updated_at: string;
}

/**
 * A minted MCP key, as it is listed afterwards.
 *
 * There is no token field here on purpose. The plaintext token exists exactly
 * once, in the response to mintKey(); the server keeps only a sha256 hash, so
 * nothing can ever hand it back. Any UI that shows a key shows it at mint time
 * or not at all.
 */
export interface McpKeyRow {
  id: number;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

/** One row of the tools/call audit trail for a connection. */
export interface ActivityRow {
  id: number;
  connector: string;
  tool_name: string;
  status: string;
  duration_ms: number | null;
  error_message: string;
  created_at: string;
}

export interface CreateConnectionPayload {
  connector: string;
  name: string;
  creds: Record<string, string>;
}

export interface UpdateConnectionPayload {
  name?: string;
  creds?: Record<string, string>;
}

/** The whole catalogue, including connectors this tenant has never touched. */
export function listConnectors(): Promise<ConnectorSpec[]> {
  return request<ConnectorSpec[]>("/connectors/", {
    method: "GET",
    authenticated: true,
  });
}

/** One connector with its tools. The slug is path data, so it is encoded. */
export function getConnector(slug: string): Promise<ConnectorDetail> {
  return request<ConnectorDetail>(
    "/connectors/" + encodeURIComponent(slug) + "/",
    { method: "GET", authenticated: true }
  );
}

/**
 * This tenant's connections, optionally narrowed to one connector.
 *
 * The filter is a query parameter rather than a path segment, so an absent or
 * empty slug simply asks for everything instead of sending
 * "/connections/?connector=" and making the server decide what an empty filter
 * is supposed to mean.
 */
export function listConnections(connector?: string): Promise<Connection[]> {
  let path = "/connections/";
  if (typeof connector === "string" && connector.length > 0) {
    path += "?connector=" + encodeURIComponent(connector);
  }
  return request<Connection[]>(path, { method: "GET", authenticated: true });
}

/**
 * Create a connection. creds travels up in the body and never comes back down;
 * the caller should drop its copy the moment this resolves rather than keeping
 * a secret alive in component state for the life of the page.
 */
export function createConnection(
  body: CreateConnectionPayload
): Promise<Connection> {
  return request<Connection>("/connections/", {
    method: "POST",
    body: {
      connector: body.connector,
      name: body.name,
      creds: body.creds,
    },
    authenticated: true,
  });
}

/**
 * Rename a connection, replace its credentials, or both.
 *
 * Each key is included only when the caller actually supplied it. An undefined
 * creds would serialize away to nothing anyway, but an empty object would reach
 * the server as "clear the credentials" and silently break a live MCP endpoint
 * during what the user thought was a rename.
 */
export function updateConnection(
  id: number,
  body: UpdateConnectionPayload
): Promise<Connection> {
  const payload: Record<string, unknown> = {};
  if (typeof body.name === "string") {
    payload["name"] = body.name;
  }
  if (body.creds !== undefined) {
    payload["creds"] = body.creds;
  }
  return request<Connection>("/connections/" + id + "/", {
    method: "PATCH",
    body: payload,
    authenticated: true,
  });
}

/**
 * Delete a connection. Its keys go with it, so every MCP client pointed at that
 * URL stops working the instant this resolves. Confirm before calling it.
 */
export async function deleteConnection(id: number): Promise<void> {
  await request<null>("/connections/" + id + "/", {
    method: "DELETE",
    authenticated: true,
  });
}

/** The connector's tools with this connection's enabled flag resolved. */
export function listConnectionTools(id: number): Promise<ConnectorTool[]> {
  return request<ConnectorTool[]>("/connections/" + id + "/tools/", {
    method: "GET",
    authenticated: true,
  });
}

/**
 * Enable or disable one tool on one connection.
 *
 * The server answers with the whole refreshed list rather than the single row,
 * so the caller replaces its state wholesale instead of patching one entry in
 * place. That keeps the UI honest when the server disagreed with the request --
 * a tool the connector no longer exposes, say -- and it is why this resolves to
 * ConnectorTool[] rather than void.
 */
export function toggleConnectionTool(
  id: number,
  tool: string,
  enabled: boolean
): Promise<ConnectorTool[]> {
  return request<ConnectorTool[]>("/connections/" + id + "/tools/", {
    method: "POST",
    body: { tool: tool, enabled: enabled },
    authenticated: true,
  });
}

/** Keys minted for this connection. Prefixes only -- see McpKeyRow. */
export function listKeys(id: number): Promise<McpKeyRow[]> {
  return request<McpKeyRow[]>("/connections/" + id + "/keys/", {
    method: "GET",
    authenticated: true,
  });
}

/**
 * Mint a key. The resolved value carries token, the full "hc_" secret, and this
 * is the only moment it will ever exist -- the server stores a sha256 hash and
 * nothing else. Show it once, offer a copy, and persist it nowhere: not browser
 * storage, not a URL, not a logged object.
 */
export function mintKey(
  id: number,
  label: string
): Promise<McpKeyRow & { token: string }> {
  return request<McpKeyRow & { token: string }>(
    "/connections/" + id + "/keys/",
    {
      method: "POST",
      body: { label: label },
      authenticated: true,
    }
  );
}

/** Revoke a key. The row survives with revoked_at stamped; the token dies. */
export async function revokeKey(id: number, keyId: number): Promise<void> {
  await request<null>("/connections/" + id + "/keys/" + keyId + "/", {
    method: "DELETE",
    authenticated: true,
  });
}

/**
 * Recent tools/call rows for a connection, newest first.
 *
 * limit is sent only when the caller asked for one, so the server's own default
 * stays the single source of truth for what "recent" means instead of being
 * duplicated as a magic number on this side of the wire.
 */
export function listConnectionActivity(
  id: number,
  limit?: number
): Promise<ActivityRow[]> {
  let path = "/connections/" + id + "/activity/";
  if (typeof limit === "number" && limit > 0) {
    path += "?limit=" + String(Math.floor(limit));
  }
  return request<ActivityRow[]>(path, { method: "GET", authenticated: true });
}

/* ------------------------------------------------------------------ */
/* Google OAuth                                                        */
/*                                                                     */
/*   GET /connectors/<slug>/oauth/start/ -> {authorize_url}            */
/*                                                                     */
/* Connectors whose auth is "google_oauth" have no credentials to      */
/* paste: the connection is created by the callback, not by this       */
/* client. So there is deliberately no matching finish() call here --  */
/* Google redirects to the server, the server redirects back to the    */
/* connector page with ?connected=1 or ?error=<message>, and the page  */
/* reads that instead of polling for a result it cannot see.           */
/* ------------------------------------------------------------------ */

/**
 * Ask the server where to send the browser to start the Google consent
 * screen. Each call mints a fresh one-time state nonce, so the URL is good for
 * exactly one attempt and must be navigated to rather than stored or reused.
 *
 * Rejects with the server's own message when Google is not configured on this
 * deployment; that message names the redirect URI an admin has to register, so
 * it must be shown verbatim rather than replaced with a friendlier sentence.
 */
export function startGoogleOAuth(
  slug: string
): Promise<{ authorize_url: string }> {
  return request<{ authorize_url: string }>(
    "/connectors/" + encodeURIComponent(slug) + "/oauth/start/",
    { method: "GET", authenticated: true }
  );
}

/* ================================================================== */
/* Activity                                                            */
/*                                                                     */
/* The workspace-wide view of the audit trail that                     */
/* listConnectionActivity() reads one connection at a time. Both are   */
/* needed and neither replaces the other: the connector page asks      */
/* "what has this connection been doing", the Overview asks "what has  */
/* been happening here at all".                                        */
/*                                                                     */
/* The Overview's answer is a server query rather than a fan-out over  */
/* listConnectionActivity(), for two reasons. A client-side merge      */
/* would cost one request per connection to build a list of twelve     */
/* rows, and it would silently lose every call made through a          */
/* connection that has since been deleted -- the activity row outlives */
/* its connection, so those rows belong to the tenant, not to any      */
/* connection that could still be listed.                              */
/*                                                                     */
/* Both calls are tenant-scoped by the server, like everything above:  */
/* there is no tenant parameter here to get wrong.                     */
/*                                                                     */
/* Contract:                                                           */
/*   GET /activity/?limit=<n>        -> ActivityEvent[]  newest first  */
/*   GET /activity/summary/?days=<n> -> ActivitySummary                */
/* ================================================================== */

/**
 * One tools/call across the whole workspace.
 *
 * The per-connection ActivityRow's wider sibling: it names the connection as
 * well as the connector, because a list that spans connections has to say
 * which one a call went through.
 *
 * connection and connection_name are BOTH null for exactly one
 * reason -- the connection was deleted after the call was made. The row is
 * kept anyway, since an audit trail that forgets what happened the moment
 * someone removes a connection is not an audit trail. Render such a row
 * without a link rather than linking to a connection that is gone.
 *
 * status is "ok" or "error", and duration_ms is null when the call failed
 * before it could be timed. Both are typed as they arrive rather than as a
 * union or a number, because narrowing a value that comes off the wire is a
 * promise this file cannot keep.
 */
export interface ActivityEvent {
  id: number;
  connector: string;
  connector_label: string;
  connection: number | null;
  // Null, not "", once the connection has been deleted -- the server nulls
  // this and `connection` together. Declaring it non-nullable is what let a
  // null-unsafe read of it compile clean.
  connection_name: string | null;
  tool_name: string;
  status: string;
  duration_ms: number | null;
  error_message: string;
  created_at: string;
}

/**
 * One day's calls, split by outcome. date is the calendar day in ISO
 * "YYYY-MM-DD" form; ok and error are counts, so ok + error is that day's
 * total and there is no third state to account for.
 */
export interface ActivityDay {
  date: string;
  ok: number;
  error: number;
}

/**
 * The counts behind a sparkline.
 *
 * days is dense: the server emits an entry for every day in the window,
 * including the quiet ones at zero, so a chart can plot it straight without
 * inventing the gaps. total and errors are the server's own sums over that
 * same window -- read them rather than re-adding days, so the number under a
 * chart can never disagree with the chart.
 */
export interface ActivitySummary {
  days: ActivityDay[];
  total: number;
  errors: number;
}

/**
 * The most recent tool calls in this workspace, newest first.
 *
 * limit travels only when the caller asked for one, leaving the server's
 * default as the single definition of "recent" instead of duplicating a magic
 * number on this side of the wire.
 */
export function listActivity(limit?: number): Promise<ActivityEvent[]> {
  let path = "/activity/";
  if (typeof limit === "number" && limit > 0) {
    path += "?limit=" + String(Math.floor(limit));
  }
  return request<ActivityEvent[]>(path, { method: "GET", authenticated: true });
}

/** Per-day call counts for the trailing window. days is omitted the same way. */
export function activitySummary(days?: number): Promise<ActivitySummary> {
  let path = "/activity/summary/";
  if (typeof days === "number" && days > 0) {
    path += "?days=" + String(Math.floor(days));
  }
  return request<ActivitySummary>(path, { method: "GET", authenticated: true });
}
