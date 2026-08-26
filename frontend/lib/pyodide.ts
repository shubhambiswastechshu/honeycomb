/**
 * Loading Pyodide -- CPython compiled to WebAssembly -- into the page.
 *
 * WHY THE BROWSER
 * ---------------
 * Running submitted Python needs a sandbox, and the honest server-side options
 * are a container or a microVM per run, with a scheduler, a resource budget and
 * a security boundary to keep maintaining. `exec()` against a restricted
 * namespace is not one of them: every version of that idea, RestrictedPython
 * included, has been escaped, and an escape on this server is a shell beside
 * every tenant's data.
 *
 * The browser already ships a sandbox that Google, Mozilla and Apple maintain
 * full time. Code that runs in it cannot reach the Django process, the
 * database, the filesystem, or any host the page's own origin policy would not
 * already allow. It also cannot reach the customer's warehouse -- which is the
 * real limit of this design, and the point at which server-side execution has
 * to be built properly rather than approximated.
 *
 * WHAT THIS COSTS
 * ---------------
 * The runtime is roughly 10MB and comes from a CDN, so it is fetched on the
 * first Run rather than on page load: most visits to the page do not run
 * anything, and 10MB spent on arrival is 10MB spent on nothing. The browser
 * caches it afterwards, so the wait is once per browser, not once per visit.
 *
 * There is exactly one loader promise for the lifetime of the tab. Two Run
 * clicks a second apart must not start two runtimes, and a failed load must not
 * be cached as a permanent failure -- so the promise is cleared on rejection
 * and the next attempt starts clean.
 */

/** Pinned. An unpinned CDN path means a Python upgrade lands without a deploy. */
export const PYODIDE_VERSION = "0.26.4";
const CDN = "https://cdn.jsdelivr.net/pyodide/v" + PYODIDE_VERSION + "/full/";

export interface PyodideInterface {
  runPythonAsync: (code: string) => Promise<unknown>;
  loadPackagesFromImports: (code: string) => Promise<unknown>;
  setStdout: (options: { batched: (text: string) => void }) => void;
  setStderr: (options: { batched: (text: string) => void }) => void;
  /** Makes a JS object importable from Python. See lib/pybridge.ts. */
  registerJsModule: (name: string, module: object) => void;
  globals: { clear?: () => void };
  version: string;
}

type LoadPyodide = (options: { indexURL: string }) => Promise<PyodideInterface>;

declare global {
  interface Window {
    loadPyodide?: LoadPyodide;
  }
}

let pending: Promise<PyodideInterface> | null = null;

function loadScript(src: string): Promise<void> {
  return new Promise(function attach(resolve, reject) {
    const existing = document.querySelector<HTMLScriptElement>(
      'script[data-pyodide="1"]'
    );
    if (existing !== null) {
      // Already in the document from a previous attempt. If the global is
      // there the script finished; otherwise wait for the same tag rather than
      // adding a second one.
      if (window.loadPyodide !== undefined) {
        resolve();
        return;
      }
      existing.addEventListener("load", function done() {
        resolve();
      });
      existing.addEventListener("error", function failed() {
        reject(new Error("The Python runtime could not be downloaded."));
      });
      return;
    }

    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.dataset.pyodide = "1";
    script.addEventListener("load", function done() {
      resolve();
    });
    script.addEventListener("error", function failed() {
      // Almost always offline, or a network that blocks the CDN. Saying which
      // host is missing is what makes that diagnosable.
      reject(
        new Error(
          "Could not reach " +
            CDN +
            " -- the Python runtime is downloaded from there on first use."
        )
      );
    });
    document.head.appendChild(script);
  });
}

/** The shared runtime, started on first call. Safe to call from anywhere. */
export function getPyodide(): Promise<PyodideInterface> {
  if (pending !== null) {
    return pending;
  }
  pending = loadScript(CDN + "pyodide.js")
    .then(function start() {
      const loader = window.loadPyodide;
      if (loader === undefined) {
        throw new Error("The Python runtime loaded but did not start.");
      }
      return loader({ indexURL: CDN });
    })
    .catch(function reset(error: unknown) {
      // Not cached as a failure: someone whose wifi dropped should be able to
      // press Run again and have it work.
      pending = null;
      throw error;
    });
  return pending;
}

/** Whether the runtime is already up, so the UI can say "first run downloads ~10MB". */
export function isPyodideReady(): boolean {
  return pending !== null && typeof window !== "undefined" && window.loadPyodide !== undefined;
}
