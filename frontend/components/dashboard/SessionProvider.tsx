"use client";

/**
 * Holds the signed-in identity for the whole dashboard subtree.
 *
 * The shell never renders with empty values: while GET /auth/me/ is in flight
 * the provider shows the shared loading screen, and if the call fails after the
 * api layer has already spent its one refresh attempt, the user is sent to
 * /signin with a "next" pointer back to the page they asked for. Nothing about
 * the session is stored in the browser -- the tokens are httpOnly cookies, so
 * this is the only place the frontend learns who it is talking to.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { me, signOut as apiSignOut } from "@/lib/api";
import type { Session } from "@/lib/api";

export interface SessionContextValue {
  /** Always present inside the provider: children only mount once it resolved. */
  session: Session;
  /** True while a refresh() is in flight; false for the first paint of children. */
  loading: boolean;
  /** Message from the last failed refresh, or null. */
  error: string | null;
  /** Re-reads /auth/me/, e.g. after the profile or organization was renamed. */
  refresh: () => Promise<void>;
  /** Clears the auth cookies server-side, then leaves for /signin. */
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

const UNKNOWN_ERROR = "Could not load your session.";

export default function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Kept in refs so load() does not change identity on every navigation --
  // otherwise the mount effect would re-fetch the identity on each route change.
  const pathnameRef = useRef<string>(pathname);
  pathnameRef.current = pathname;
  const aliveRef = useRef<boolean>(true);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const load = useCallback(
    async function load(): Promise<void> {
      try {
        const next = await me();
        if (!aliveRef.current) {
          return;
        }
        setSession(next);
        setError(null);
      } catch (caught) {
        if (!aliveRef.current) {
          return;
        }
        setSession(null);
        setError(caught instanceof Error ? caught.message : UNKNOWN_ERROR);
        // End the session server-side before leaving. The middleware decides
        // "signed in" purely from the presence of the hc_access cookie, so a
        // cookie that is well-formed but no longer usable (deactivated user,
        // backend down, 429, 5xx) would bounce us straight back here and the
        // two would trade redirects forever. POST /auth/logout/ is AllowAny
        // and just clears the cookies, so it works even for a dead token; if
        // it fails there is nothing more to do, and the middleware change that
        // honours an explicit ?next= keeps the loop broken anyway.
        await apiSignOut().catch(function ignore() {});
        const target = pathnameRef.current || "/dashboard";
        router.replace("/signin?next=" + encodeURIComponent(target));
      } finally {
        if (aliveRef.current) {
          setLoading(false);
        }
      }
    },
    [router]
  );

  useEffect(
    function loadOnMount() {
      void load();
    },
    [load]
  );

  const refresh = useCallback(
    async function refresh(): Promise<void> {
      setLoading(true);
      await load();
    },
    [load]
  );

  const signOut = useCallback(
    async function signOut(): Promise<void> {
      try {
        await apiSignOut();
      } catch (caught) {
        // The cookies are the server's to clear; if the call failed the user
        // still asked to leave, so send them to /signin either way.
      }
      if (aliveRef.current) {
        setSession(null);
      }
      router.replace("/signin");
    },
    [router]
  );

  if (session === null) {
    if (loading) {
      return (
        <div className="dash-boot">
          <LoadingScreen label="Loading" />
        </div>
      );
    }
    // The redirect is already queued; render neutral ground rather than a
    // flash of dashboard chrome built on values we do not have.
    return <div className="dash-boot" />;
  }

  return (
    <SessionContext.Provider
      value={{
        session: session,
        loading: loading,
        error: error,
        refresh: refresh,
        signOut: signOut,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}

/** Reads the dashboard session. Only valid below <SessionProvider>. */
export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error(
      "useSession() was called outside <SessionProvider>. Wrap the component in the dashboard layout's SessionProvider."
    );
  }
  return value;
}
