import { redirect } from "next/navigation";

/**
 * The middleware already routes "/" to /dashboard or /signin depending on the
 * hc_access cookie, so this component only runs if that guard is bypassed.
 * Sending an unknown visitor to sign in is the safe fallback.
 */
export default function RootPage() {
  redirect("/signin");
}
