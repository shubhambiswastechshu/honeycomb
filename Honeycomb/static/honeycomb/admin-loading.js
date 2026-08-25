/**
 * Drives the Honeycomb ripple across admin page loads.
 *
 * The admin is server-rendered, so a click or a submit means the browser is
 * about to throw this document away and wait on Django. That wait is the only
 * thing worth covering.
 *
 *   1. A click or submit that will navigate arms a timer.
 *   2. If the page is still here SHOW_AFTER_MS later, the cover fades up and
 *      holds until the new document replaces it.
 *   3. Anything that cancels the navigation -- failed validation, a bfcache
 *      restore -- disarms it.
 *
 * Nothing happens on arrival, and that is the point. Two earlier versions were
 * both wrong: fading in on click let the browser destroy the page mid-fade, so
 * every click read as cover / blank / new page; covering the incoming document
 * too removed that seam but put a white curtain over every load including the
 * ones that finish in 15ms -- a flick for a wait that was never there. Under
 * the threshold the browser's own page swap is the transition, which on
 * localhost is every navigation.
 *
 * The overlay markup lives in the template rather than being built here so the
 * cover is one class away at all times, with no DOM work between the click and
 * the paint.
 */
(function () {
  "use strict";

  var root = document.documentElement;

  // Below this the page has usually already swapped, and a cover would be a
  // flash rather than a loading state. Above it, the wait is long enough that
  // showing nothing reads as a dead click.
  var SHOW_AFTER_MS = 250;

  var timer = null;

  function uncover() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    root.classList.remove("hc-leaving");
  }

  function arm() {
    if (timer !== null) {
      return;
    }
    timer = window.setTimeout(function show() {
      timer = null;
      root.classList.add("hc-leaving");
    }, SHOW_AFTER_MS);
  }

  /** True for a click the browser will not turn into a navigation. */
  function isModifiedClick(event) {
    return (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    );
  }

  /** True for a link that leaves this document standing. */
  function staysOnPage(link) {
    if (link.hasAttribute("download") || link.target === "_blank") {
      return true;
    }
    var href = link.getAttribute("href");
    if (href === null || href === "" || href.charAt(0) === "#") {
      return true;
    }
    // javascript: and mailto: run in place; the admin uses the former for its
    // popup pickers and the calendar/clock widgets.
    if (link.protocol === "javascript:" || link.protocol === "mailto:") {
      return true;
    }
    if (link.origin !== window.location.origin) {
      return true;
    }
    // Same page, different fragment: no request goes out.
    return (
      link.pathname === window.location.pathname &&
      link.search === window.location.search &&
      link.hash !== ""
    );
  }

  document.addEventListener(
    "click",
    function onClick(event) {
      if (isModifiedClick(event)) {
        return;
      }
      var link = event.target.closest ? event.target.closest("a") : null;
      if (link === null || staysOnPage(link)) {
        return;
      }
      // The related-object widgets open in a popup and leave this page alone.
      if (link.classList.contains("related-widget-wrapper-link")) {
        return;
      }
      arm();
    },
    true
  );

  document.addEventListener(
    "submit",
    function onSubmit(event) {
      if (event.defaultPrevented) {
        return;
      }
      var form = event.target;
      if (form.target === "_blank" || form.hasAttribute("data-hc-no-loader")) {
        return;
      }
      arm();
    },
    true
  );

  // A form that fails client-side validation never navigates, and the browser
  // fires this instead of submit -- without it the cover would be left up over
  // a page that is going nowhere.
  document.addEventListener("invalid", uncover, true);

  // Coming back via the back button restores this document from the bfcache
  // with whatever classes it had when it left -- including hc-leaving, which
  // would otherwise cover a page that has already finished arriving.
  window.addEventListener("pageshow", uncover);
})();
