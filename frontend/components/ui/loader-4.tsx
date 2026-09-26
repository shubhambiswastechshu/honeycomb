/**
 * Honeycomb ripple loader.
 *
 * Seven hexagons in the classic comb cluster (one center cell ringed by six),
 * rippling outward from the middle. Plain CSS — see the `.loader` / `.cell`
 * rules in app/globals.css — so the app keeps its zero-dependency styling.
 */
const Loader = () => {
  return (
    <div className="loader" role="status" aria-label="Loading">
      <div className="hex-row">
        <span className="cell d-3" />
        <span className="cell d-4" />
      </div>
      <div className="hex-row">
        <span className="cell d-2" />
        <span className="cell d-0" />
        <span className="cell d-5" />
      </div>
      <div className="hex-row">
        <span className="cell d-1" />
        <span className="cell d-6" />
      </div>
    </div>
  );
};

export default Loader;
