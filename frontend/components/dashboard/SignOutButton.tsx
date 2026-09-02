"use client";

/**
 * Sign out, living in the top bar rather than on the profile page, so it is
 * reachable from every screen instead of only from one.
 *
 * It always asks first, through the app's own ConfirmDialog -- never
 * `window.confirm`. The provider's signOut() clears the cookies server-side
 * and performs the redirect itself, so this owns only the pending flag.
 */

import { useState } from "react";
import { LogOut } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import { useSession } from "@/components/dashboard/SessionProvider";

export default function SignOutButton() {
  const { signOut } = useSession();
  const [asking, setAsking] = useState<boolean>(false);
  const [leaving, setLeaving] = useState<boolean>(false);

  function confirm(): void {
    setLeaving(true);
    void signOut();
  }

  return (
    <>
      <button
        type="button"
        className="dash-signout"
        aria-label="Sign out"
        title="Sign out"
        aria-haspopup="dialog"
        onClick={function onClick() {
          setAsking(true);
        }}
      >
        <LogOut size={17} strokeWidth={1.8} aria-hidden="true" />
      </button>

      <ConfirmDialog
        open={asking}
        title="Sign out?"
        description="This ends your session on this device. Your account and everything in it are unaffected."
        confirmLabel="Sign out"
        pendingLabel="Signing out"
        destructive
        pending={leaving}
        onConfirm={confirm}
        onCancel={function onCancel() {
          setAsking(false);
        }}
      />
    </>
  );
}
