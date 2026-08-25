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
