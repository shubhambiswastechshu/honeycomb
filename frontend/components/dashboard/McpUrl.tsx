"use client";

/**
 * The MCP URL of one connection, with a copy button.
 *
 * Shared, because this is the single thing the product exists to hand over and
 * it now appears in three places -- the connector's instance list, the Access
 * tab, and the Data panel. Three copies of a clipboard fallback is three
 * places for it to drift.
 */

import { useState } from "react";
import { Check, Copy } from "lucide-react";

/**
 * Copy to the clipboard, with a fallback for a page served over plain http.
 * `navigator.clipboard` is undefined outside a secure context, which is
 * exactly where this runs in development.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard !== undefined) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (caught) {
    // Fall through to the textarea below rather than reporting failure: the
    // clipboard API rejects for reasons the fallback does not care about.
  }
  try {
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(field);
    return ok;
  } catch (caught) {
    return false;
  }
}

export interface McpUrlProps {
  url: string;
  /** Announced to a screen reader, since the button itself only says "Copy". */
  label?: string;
}

export default function McpUrl({ url, label }: McpUrlProps) {
  const [copied, setCopied] = useState<boolean>(false);

  function onCopy(): void {
    void copyText(url).then(function done(ok: boolean) {
      setCopied(ok);
      if (ok) {
        window.setTimeout(function reset() {
          setCopied(false);
        }, 2000);
      }
    });
  }

  return (
    <div className="conn-url">
      <code className="conn-url-text">{url}</code>
      <button
        type="button"
        className="conn-copy"
        onClick={onCopy}
        aria-label={
          typeof label === "string" ? label : "Copy the MCP URL"
        }
      >
        {copied ? (
          <Check size={13} strokeWidth={2.3} aria-hidden="true" />
        ) : (
          <Copy size={13} strokeWidth={1.9} aria-hidden="true" />
        )}
        <span>{copied ? "Copied" : "Copy"}</span>
      </button>
    </div>
  );
}
