import Loader from "@/components/ui/loader-4";

/** Full-bleed loading state: the ripple grid centered on the page. */
export default function LoadingScreen({ label }: { label?: string }) {
  return (
    <div className="loading-screen">
      <Loader />
      {label ? <p className="loading-label">{label}</p> : null}
    </div>
  );
}
