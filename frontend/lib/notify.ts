/**
 * Desktop notifications for things that went wrong.
 *
 * Chrome, Edge and Firefox all expose the same Notification API, and it is the
 * whole of what this file wraps. Every call is guarded, because the API throws
 * or is missing in more places than it is documented to be: Safari before 16.4,
 * any page not on HTTPS, an iframe without `allow="notifications"`, and a
 * browser where the user has blocked them at the OS level rather than the site
 * level. A dashboard must not break because a notification could not be shown.
 *
 * WHAT THIS CANNOT DO, and the UI says so rather than implying otherwise: a
 * notification only fires while a tab with this page is open. It may be in the
 * background, behind other windows, or minimised -- that all works -- but close
 * the last tab and nothing is delivered. Alerting with the browser shut needs a
 * service worker and Web Push, which needs VAPID keys on the server and a
 * subscription stored per user. That is a different feature, not a flag on this
 * one.
 */

/** Whether this browser offers the API at all. False during server rendering. */
export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

/**
 * The current permission, or "unsupported" where there is no API to ask.
 *
 * Returned as a plain string rather than NotificationPermission so callers can
 * handle the unsupported case in the same switch as the three real ones.
 */
export function notificationPermission(): NotificationPermission | "unsupported" {
  if (!notificationsSupported()) {
    return "unsupported";
  }
  try {
    return Notification.permission;
  } catch {
    return "unsupported";
  }
}

/**
 * Ask for permission, returning what the user chose.
 *
 * Browsers require this to be called from a user gesture -- a click -- or they
 * reject it without showing anything. It is called from the toggle's onClick
 * for that reason, never on mount.
 */
export async function requestNotifications(): Promise<
  NotificationPermission | "unsupported"
> {
  if (!notificationsSupported()) {
    return "unsupported";
  }
  try {
    // Older Safari passes a callback instead of returning a promise. Promise
    // .resolve copes with both without a feature test.
    return await Promise.resolve(Notification.requestPermission());
  } catch {
    return "unsupported";
  }
}

/** What a notification needs to say. `tag` collapses repeats of the same thing. */
export interface FailureNotice {
  title: string;
  body: string;
  /** Same tag replaces an earlier notification instead of stacking another. */
  tag: string;
}

/**
 * Show one notification, if we are allowed to.
 *
 * Returns whether it was shown, which the caller uses only to decide whether to
 * keep its own "already told them" bookkeeping -- it never surfaces as an error,
 * because a notification that could not be shown is not a failure of the page.
 */
export function notifyFailure(notice: FailureNotice): boolean {
  if (notificationPermission() !== "granted") {
    return false;
  }
  try {
    const shown = new Notification(notice.title, {
      body: notice.body,
      tag: notice.tag,
      // The page is already showing this; the notification is for when it is
      // not the window in front. Silent would defeat the point of asking.
      silent: false,
    });
    // Bring the dashboard forward when the notification is clicked. Wrapped
    // because window.focus() is refused in some embedded contexts.
    shown.onclick = function focusDashboard(): void {
      try {
        window.focus();
      } catch {
        /* Nothing to do: the notification still closes. */
      }
      shown.close();
    };
    return true;
  } catch {
    return false;
  }
}
