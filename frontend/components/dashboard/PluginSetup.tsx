"use client";

import { Download } from "lucide-react";

import { API_BASE } from "@/lib/api";

/**
 * How to get the WordPress plugin, shown where you are asked for its token.
 *
 * The WordPress connector wants a site URL and an API token "from the TechShu
 * SEO Bridge plugin settings". Until now the product never said where that
 * plugin came from -- it lived in a repository the user has no access to -- so
 * the first field of the form asked for something unobtainable.
 *
 * This sits above those fields rather than in a help page, because the moment
 * someone needs it is the moment they are reading them.
 */

/** Built by connections/plugin.py from the source vendored in this repo. */
const PLUGIN_URL = API_BASE + "/plugins/wordpress/";

/* The download is the button above, so it is not step one as well. */
const STEPS = [
  "In WordPress: Plugins → Add New → Upload Plugin, choose the zip, then Install and Activate.",
  "Open Settings → TechShu SEO Bridge and copy the token it shows.",
  "Paste your site address and that token below.",
];

export default function PluginSetup() {
  return (
    <section className="plug" aria-labelledby="plug-title">
      <div className="plug-head">
        <div>
          <h3 className="plug-title" id="plug-title">
            First, install the bridge plugin
          </h3>
          <p className="plug-lede">
            WordPress has no way to hand out API access on its own, so this
            plugin adds it and hands you a token.
          </p>
        </div>
        {/* A plain anchor with download: the file is served by the API host and
            the browser should save it, not navigate to it. */}
        <a className="conn-button conn-button-primary plug-get" href={PLUGIN_URL} download>
          <Download size={14} strokeWidth={2} aria-hidden="true" />
          Download plugin
        </a>
      </div>

      <ol className="plug-steps">
        {STEPS.map(function each(step, index) {
          return (
            <li key={index}>
              <span className="plug-n" aria-hidden="true">
                {index + 1}
              </span>
              <span>{step}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
