=== TechShu SEO Bridge ===
Contributors: shubhambiswas2212
Tags: ai, seo, content, automation, woocommerce
Requires at least: 5.6
Tested up to: 7.0
Requires PHP: 7.4
Stable tag: 2.1.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Connect your WordPress site to the Falcon MCP portal so AI assistants you authorize can read your site and propose or apply content & SEO changes.

== Description ==

TechShu SEO Bridge links your WordPress site to **Falcon** (a hosted MCP — Model
Context Protocol — portal operated by TechShu) so AI assistants such as Claude or
ChatGPT, which you connect and authorize, can help you manage the site: read posts
and Yoast-compatible SEO meta, propose better titles / meta descriptions / internal
links, and (when you allow it) manage media, menus, themes, users, settings,
performance, backups and WooCommerce.

It exposes a small, token-secured REST bridge at `/wp-json/falcon/v1/*`. Only
requests carrying the bearer token you generate in WP-admin can use it.

= Key features =

* Read posts/pages and their Yoast-compatible SEO meta.
* Stage SEO changes (title, meta description, focus keyword, internal links) for
  human approval before they go live.
* Optional live management of media, menus, themes (incl. FSE), users, settings,
  security hardening, performance, backups and WooCommerce.
* Every change is logged in WP-admin → TechShu SEO Bridge.

== External services ==

This plugin connects your site to **Falcon**, a hosted service operated by
TechShu, reachable at https://bringdata.a.techshu.in (API: https://falcon-api.a.techshu.in).
The connection exists so AI assistants you explicitly authorize can read and
manage your site through Falcon.

* **What is sent, and when:** only in response to authenticated requests that
  carry the API token you generate in WP-admin → TechShu SEO Bridge. Depending on
  the action you or your authorized AI client triggers, the plugin sends the
  relevant site data — for example post/page content and SEO meta, media
  metadata, plugin/theme/user/settings information, or WooCommerce data — to the
  Falcon API endpoint you connected. Nothing is sent unless such an authenticated
  request is made; the plugin does not phone home on its own.
* **Why:** to let your authorized AI client read and edit your site remotely
  through the Falcon portal.
* **Terms of Service:** https://bringdata.a.techshu.in/terms
* **Privacy Policy:** https://bringdata.a.techshu.in/privacy

By generating a token and connecting the site you consent to this data exchange.
Revoke access any time by regenerating or deleting the token in WP-admin.

This plugin also optionally talks to Google's Indexing API, only if you choose to
submit a Google service-account and use the indexing tools:

* **What is sent, and when:** the service-account JSON you provide (stored so the
  plugin can request an access token), and — only when you or your AI client
  explicitly submits a URL to index — that URL and its indexing status.
  Nothing is sent to Google unless you first configure a service account and then
  trigger an indexing action.
* **Why:** to let you ask Google to (re)crawl or drop a URL from its index via the
  official Indexing API (`oauth2.googleapis.com`, `indexing.googleapis.com`).
* **Terms of Service:** https://policies.google.com/terms
* **Privacy Policy:** https://policies.google.com/privacy

== Installation ==

1. Upload the plugin via **Plugins → Add New → Upload Plugin**, or copy the
   plugin folder to `wp-content/plugins/`. Activate it.
2. Go to **WP-admin → TechShu SEO Bridge** and copy the **Site URL** and **API token**.
3. In the Falcon portal (https://bringdata.a.techshu.in) → **Connectors → + Add MCP
   → WordPress**, paste the Site URL and API token.
4. Connect the WordPress MCP to your AI client (Claude / ChatGPT).
5. Ask the AI to read your data and propose changes. Staged changes appear under
   WP-admin → TechShu SEO Bridge for you to approve or reject.

== Frequently Asked Questions ==

= Does the AI change my site without permission? =
SEO proposals are staged and only applied after a user with `manage_options`
approves them. Other management actions run only when triggered by an
authenticated request carrying your token, which you control and can revoke.

= Is an account required? =
Yes — you need a Falcon account at https://bringdata.a.techshu.in to connect the
site. The plugin itself is free and GPL-licensed.

= How do I disconnect? =
Regenerate or delete the API token in WP-admin → TechShu SEO Bridge, or
deactivate the plugin.

= Can the AI install arbitrary code on my site? =
No. Plugin/theme installs only pull from the official WordPress.org repository —
there's no way to install from an arbitrary URL. The API also can't write PHP
files into your themes or plugins, can't create administrator accounts, and can't
set core options that control site/security configuration (active plugins, the
active theme, site URL, registration defaults, etc.).

== Screenshots ==

1. The TechShu SEO Bridge admin screen showing the Site URL, API token, and the staged-changes approval queue.
2. Connecting the site in the Falcon portal.

== Changelog ==

= 2.1.0 =
* Compatibility: the connector now works on sites that block the REST API for
  non-logged-in users (a common "disable REST API" security hardening toggle /
  snippet that returns "You are not currently logged in."). A valid Falcon
  Bearer token now clears that block for its own request only — anonymous and
  wrong-token requests stay blocked, so the hardening is fully preserved. No
  site settings are changed.

= 2.0.0 =
* Renamed to TechShu SEO Bridge.
* Security hardening: removed remote code-execution surface (arbitrary zip_url
  plugin/theme installs, theme/plugin file writing, PHP code snippets) that let an
  authenticated caller place executable code on the site; theme files remain
  viewable, just no longer writable through the API.
* SMTP and hardening toggles now apply live from the database instead of writing
  generated PHP into `mu-plugins` — no credentials or generated code touch disk.
* Blocked creating or promoting accounts to Administrator through the API, and
  denylisted core options (active plugins/theme, site URL, registration defaults,
  auth salts, …) from the generic option-setter.
* The generic REST proxy is now read-only (GET only).
* The public, unauthenticated `/selftest` endpoint no longer discloses PHP
  version, filesystem state, or the active-plugin map — only reachability.
* Backup/export files now live under a per-site random folder name instead of a
  fixed, guessable one.
* Fixed a stored-XSS risk in custom JSON-LD schema output.

= 1.12.0 =
* Security suite: whole-site outdated-component scan, hardening checks, malware/secrets scan.
* WooCommerce bulk operations, Elementor and Contact Form 7 support, ACF field-group management.

== Upgrade Notice ==

= 2.0.0 =
Security hardening release — removes remote code-execution surface (arbitrary
plugin/theme installs, theme/plugin file writing, code snippets) and locks down
several admin-impersonation paths. Recommended for all sites.
