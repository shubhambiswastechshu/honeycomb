"""WordPress connector — drive a WP site (Yoast SEO) through the TechShu SEO Bridge plugin.

This pairs with the "TechShu SEO Bridge" WordPress plugin (see /wordpress-plugin). The plugin
exposes a small secured REST bridge at  {site}/wp-json/falcon/v1/*  and this connector
calls it. Writes are applied LIVE immediately (AI-controlled from Claude/ChatGPT) and
recorded to a history log visible in WP-admin → TechShu SEO Bridge.

Auth: api_key. The user pastes:
  * site_url  — the WordPress site root, e.g. https://example.com
  * api_token — the token shown on the plugin's "TechShu SEO Bridge" settings screen

Tools (read):
  site_info        : plugin status, Yoast detected?, counts
  list_posts       : posts/pages with their current Yoast title/meta/focus keyword
  get_post         : full content + Yoast meta for one post
  recent_changes   : history of SEO changes BRING-DATA has applied to the site

Tools (write — applied live, disabled by default until you enable them):
  update_seo_meta      : set Yoast title / meta description / focus keyword (live)
  insert_internal_link : add an anchor-text internal link inside a post (live)
"""
from urllib.parse import urlparse

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, request as http_request
from connections.models import Connection


def _conf(conn: Connection) -> tuple[str, str]:
    creds = conn.creds() or {}
    site = (creds.get("site_url") or creds.get("url") or "").strip().rstrip("/")
    token = (creds.get("api_token") or creds.get("token") or "").strip()
    if not site:
        raise ConnectorError("Not connected: missing site_url (your WordPress site root).")
    if not token:
        raise ConnectorError("Not connected: missing api_token (from the TechShu SEO Bridge plugin settings).")
    if not site.startswith(("http://", "https://")):
        site = "https://" + site
    if not urlparse(site).netloc:
        raise ConnectorError(f"Invalid site_url: {site}")
    return f"{site}/wp-json/falcon/v1", token


async def _call(conn: Connection, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
    base, token = _conf(conn)
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with limit_for(url):
            res = await http_request(method, url, headers=headers, params=params or None, json=json)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code in (401, 403):
        # Only the plugin's OWN auth errors carry these codes. A 401/403 without them
        # almost always means a security layer (Wordfence / Cloudflare / WAF) blocked us
        # BEFORE the request reached the plugin — a totally different fix than the token.
        body = res.text or ""
        if "falcon_bad_token" in body or "falcon_no_token" in body:
            raise ConnectorError("WordPress rejected the token. Re-copy it from the TechShu SEO Bridge plugin settings screen.")
        raise ConnectorError(
            f"The site blocked our request (HTTP {res.status_code}) before it reached the plugin — "
            "this is usually a security plugin or firewall (Wordfence, Cloudflare, WAF/ModSecurity), "
            "not a token problem. Allowlist BRING-DATA (User-Agent 'TechShu-Connect-MCP' or our server IP) on the site, "
            "then retry."
        )
    if res.status_code == 404:
        raise ConnectorError(
            "Endpoint not found. Is the 'TechShu SEO Bridge' plugin installed & active, and is site_url correct?"
        )
    if res.status_code >= 400:
        raise ConnectorError(f"WordPress error {res.status_code}: {res.text[:300]}")
    try:
        return res.json()
    except ValueError:
        raise ConnectorError(f"WordPress returned non-JSON: {res.text[:300]}")


def _post_id(args: dict) -> int:
    pid = args.get("post_id") or args.get("id")
    try:
        return int(pid)
    except (TypeError, ValueError):
        raise ConnectorError("`post_id` is required (integer).")


# ============================================================
# Read tools
# ============================================================

async def site_info(conn: Connection, db, args: dict) -> dict:
    """Plugin status, WordPress/site info, whether Yoast is active, and post counts."""
    return await _call(conn, "GET", "/site")


async def list_posts(conn: Connection, db, args: dict) -> dict:
    """List posts/pages with their current Yoast SEO title, meta description and focus keyword."""
    params = {
        "search": (args.get("search") or "").strip() or None,
        "type": (args.get("type") or "post").strip(),
        "per_page": max(1, min(int(args.get("per_page") or 20), 100)),
        "page": max(1, int(args.get("page") or 1)),
    }
    params = {k: v for k, v in params.items() if v is not None}

    async def _loader():
        return await _call(conn, "GET", "/posts", params=params)

    return await cached("wordpress", conn.id, "list_posts", TTL_SHORT, _loader, args=params)


async def get_post(conn: Connection, db, args: dict) -> dict:
    """Full content + Yoast meta for one post/page."""
    pid = _post_id(args)

    async def _loader():
        return await _call(conn, "GET", f"/posts/{pid}")

    return await cached("wordpress", conn.id, "get_post", TTL_SHORT, _loader, args={"id": pid})


async def recent_changes(conn: Connection, db, args: dict) -> dict:
    """History of SEO changes BRING-DATA has applied to the site (most recent first)."""
    return await _call(conn, "GET", "/pending")


# ============================================================
# Write tools (staged for human approval)
# ============================================================

async def update_seo_meta(conn: Connection, db, args: dict) -> dict:
    """Set new Yoast SEO title / meta description / focus keyword for a post.

    The change is applied LIVE immediately and logged to the WP-admin 'TechShu SEO Bridge'
    history. At least one of the three fields must be supplied.
    """
    pid = _post_id(args)
    payload = {}
    for key in ("title", "meta_description", "focus_keyword"):
        v = args.get(key)
        if v is not None:
            payload[key] = str(v)
    if not payload:
        raise ConnectorError("Provide at least one of: title, meta_description, focus_keyword.")
    payload["reason"] = (args.get("reason") or "").strip()
    return await _call(conn, "POST", f"/posts/{pid}/seo", json=payload)


async def insert_internal_link(conn: Connection, db, args: dict) -> dict:
    """Add an anchor-text internal link inside a post (applied live)."""
    pid = _post_id(args)
    anchor = (args.get("anchor_text") or "").strip()
    target = (args.get("target_url") or "").strip()
    if not anchor or not target:
        raise ConnectorError("Both `anchor_text` and `target_url` are required.")
    payload = {"anchor_text": anchor, "target_url": target, "reason": (args.get("reason") or "").strip()}
    return await _call(conn, "POST", f"/posts/{pid}/link", json=payload)


# ============================================================
# Content management
# ============================================================

def _pick(args: dict, *keys) -> dict:
    out = {}
    for k in keys:
        if args.get(k) is not None:
            out[k] = args[k]
    return out


async def create_post(conn: Connection, db, args: dict) -> dict:
    """Create a new post or page (live). title required; content is HTML.

    Landing pages: set full_html=true to keep your own <style>/<script> and inline CSS
    (otherwise WordPress strips them), and set template to a full-width/canvas page
    template (discover available ones with list_page_templates) so it renders without
    the theme's header/footer/sidebar.
    """
    if not (args.get("title") or "").strip():
        raise ConnectorError("`title` is required.")
    body = _pick(args, "title", "content", "excerpt", "status", "type", "categories", "tags",
                 "featured_image_url", "full_html", "template", "slug", "reason")
    return await _call(conn, "POST", "/posts/create", json=body)


async def update_post(conn: Connection, db, args: dict) -> dict:
    """Edit an existing post/page (live). Any of title/content/excerpt/status/categories/tags.

    Use full_html=true to preserve a self-contained HTML/CSS layout (else <style>/<script>
    are stripped); template sets a full-width/canvas page template (see list_page_templates).
    """
    pid = _post_id(args)
    body = _pick(args, "title", "content", "excerpt", "status", "categories", "tags",
                 "featured_image_url", "full_html", "template", "slug", "reason")
    if not body:
        raise ConnectorError("Provide at least one field to update.")
    return await _call(conn, "POST", f"/posts/{pid}/update", json=body)


async def delete_post(conn: Connection, db, args: dict) -> dict:
    """Trash a post/page, or permanently delete it with force=true (live)."""
    pid = _post_id(args)
    return await _call(conn, "POST", f"/posts/{pid}/delete", json={"force": bool(args.get("force")), "reason": (args.get("reason") or "")})


async def get_post_revisions(conn: Connection, db, args: dict) -> dict:
    """List a post's revision history."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/revisions")


# ============================================================
# Media library
# ============================================================

async def upload_media(conn: Connection, db, args: dict) -> dict:
    """Import an image/file into the Media Library from a URL (live)."""
    url = (args.get("url") or "").strip()
    if not url:
        raise ConnectorError("`url` (the image/file URL to import) is required.")
    body = _pick(args, "url", "title", "alt", "post_id", "reason")
    return await _call(conn, "POST", "/media/upload", json=body)


async def list_media(conn: Connection, db, args: dict) -> dict:
    """Browse the Media Library."""
    params = {"per_page": max(1, min(int(args.get("per_page") or 20), 100)), "page": max(1, int(args.get("page") or 1))}
    return await _call(conn, "GET", "/media", params=params)


async def delete_media(conn: Connection, db, args: dict) -> dict:
    """Permanently delete a media item (live)."""
    mid = int(args.get("media_id") or args.get("id") or 0)
    if not mid:
        raise ConnectorError("`media_id` is required.")
    return await _call(conn, "POST", f"/media/{mid}/delete", json={})


# ============================================================
# Categories & tags
# ============================================================

async def list_categories(conn: Connection, db, args: dict) -> dict:
    """List all post categories."""
    return await _call(conn, "GET", "/categories")


async def create_category(conn: Connection, db, args: dict) -> dict:
    """Create a new category (live)."""
    if not (args.get("name") or "").strip():
        raise ConnectorError("`name` is required.")
    return await _call(conn, "POST", "/categories/create", json=_pick(args, "name", "description", "parent", "slug"))


async def list_tags(conn: Connection, db, args: dict) -> dict:
    """List all post tags."""
    return await _call(conn, "GET", "/tags")


async def create_tag(conn: Connection, db, args: dict) -> dict:
    """Create a new tag (live)."""
    if not (args.get("name") or "").strip():
        raise ConnectorError("`name` is required.")
    return await _call(conn, "POST", "/tags/create", json=_pick(args, "name", "description"))


# ============================================================
# Menus
# ============================================================

async def list_menus(conn: Connection, db, args: dict) -> dict:
    """List registered navigation menus."""
    return await _call(conn, "GET", "/menus")


async def get_menu(conn: Connection, db, args: dict) -> dict:
    """Get the items in a navigation menu."""
    mid = int(args.get("menu_id") or args.get("id") or 0)
    if not mid:
        raise ConnectorError("`menu_id` is required.")
    return await _call(conn, "GET", f"/menus/{mid}")


async def update_menu(conn: Connection, db, args: dict) -> dict:
    """Add, remove or reorder menu items (live).

    add: [{title, url}], remove: [item_id], reorder: [item_id, ...] in desired order.
    """
    mid = int(args.get("menu_id") or args.get("id") or 0)
    if not mid:
        raise ConnectorError("`menu_id` is required.")
    body = _pick(args, "add", "remove", "reorder", "reason")
    if not any(k in body for k in ("add", "remove", "reorder")):
        raise ConnectorError("Provide at least one of: add, remove, reorder.")
    return await _call(conn, "POST", f"/menus/{mid}/update", json=body)


# ============================================================
# Plugins & themes
# ============================================================

async def list_plugins(conn: Connection, db, args: dict) -> dict:
    """List installed plugins with active/inactive status."""
    return await _call(conn, "GET", "/plugins")


async def activate_plugin(conn: Connection, db, args: dict) -> dict:
    """Activate a plugin by its file path (e.g. 'akismet/akismet.php') (live)."""
    plugin = (args.get("plugin") or "").strip()
    if not plugin:
        raise ConnectorError("`plugin` (file path, e.g. 'akismet/akismet.php') is required.")
    return await _call(conn, "POST", "/plugins/toggle", json={"plugin": plugin, "active": True, "reason": args.get("reason", "")})


async def deactivate_plugin(conn: Connection, db, args: dict) -> dict:
    """Deactivate a plugin by its file path (live)."""
    plugin = (args.get("plugin") or "").strip()
    if not plugin:
        raise ConnectorError("`plugin` (file path) is required.")
    return await _call(conn, "POST", "/plugins/toggle", json={"plugin": plugin, "active": False, "reason": args.get("reason", "")})


async def list_themes(conn: Connection, db, args: dict) -> dict:
    """List installed themes with the active one flagged."""
    return await _call(conn, "GET", "/themes")


async def activate_theme(conn: Connection, db, args: dict) -> dict:
    """Switch the active theme by its stylesheet slug (live)."""
    slug = (args.get("stylesheet") or args.get("theme") or "").strip()
    if not slug:
        raise ConnectorError("`stylesheet` (theme slug) is required.")
    return await _call(conn, "POST", "/themes/activate", json={"stylesheet": slug, "reason": args.get("reason", "")})


# ============================================================
# Users
# ============================================================

async def list_users(conn: Connection, db, args: dict) -> dict:
    """List WordPress users with their roles."""
    return await _call(conn, "GET", "/users")


async def create_user(conn: Connection, db, args: dict) -> dict:
    """Create a new WordPress user (live). username + email required."""
    if not (args.get("username") or "").strip() or not (args.get("email") or "").strip():
        raise ConnectorError("`username` and `email` are required.")
    return await _call(conn, "POST", "/users/create", json=_pick(args, "username", "email", "password", "role", "reason"))


async def update_user_role(conn: Connection, db, args: dict) -> dict:
    """Change a user's role (live). e.g. role='editor'."""
    uid = int(args.get("user_id") or args.get("id") or 0)
    role = (args.get("role") or "").strip()
    if not uid or not role:
        raise ConnectorError("`user_id` and `role` are required.")
    return await _call(conn, "POST", f"/users/{uid}/role", json={"role": role, "reason": args.get("reason", "")})


# ============================================================
# Settings
# ============================================================

async def get_site_settings(conn: Connection, db, args: dict) -> dict:
    """Read general site settings (title, tagline, timezone, etc.)."""
    return await _call(conn, "GET", "/settings")


async def update_site_settings(conn: Connection, db, args: dict) -> dict:
    """Update general site settings (live). Pass any of the allowed keys."""
    body = _pick(args, "blogname", "blogdescription", "admin_email", "timezone_string",
                 "date_format", "time_format", "start_of_week", "posts_per_page",
                 "default_category", "show_on_front", "page_on_front", "reason")
    if not body or set(body.keys()) <= {"reason"}:
        raise ConnectorError("Provide at least one setting to update.")
    return await _call(conn, "POST", "/settings/update", json=body)


# ============================================================
# Comments
# ============================================================

async def list_comments(conn: Connection, db, args: dict) -> dict:
    """List comments, optionally filtered by status (pending/approved/spam/trash/all)."""
    params = {"status": (args.get("status") or "all").strip(), "per_page": max(1, min(int(args.get("per_page") or 30), 100))}
    return await _call(conn, "GET", "/comments", params=params)


async def approve_comment(conn: Connection, db, args: dict) -> dict:
    """Approve a comment (live)."""
    cid = int(args.get("comment_id") or args.get("id") or 0)
    if not cid:
        raise ConnectorError("`comment_id` is required.")
    return await _call(conn, "POST", f"/comments/{cid}/approve", json={})


async def delete_comment(conn: Connection, db, args: dict) -> dict:
    """Delete a comment — to trash, or permanently with force=true (live)."""
    cid = int(args.get("comment_id") or args.get("id") or 0)
    if not cid:
        raise ConnectorError("`comment_id` is required.")
    return await _call(conn, "POST", f"/comments/{cid}/delete", json={"force": bool(args.get("force"))})


# ============================================================
# SEO extended
# ============================================================

async def bulk_update_seo_meta(conn: Connection, db, args: dict) -> dict:
    """Update Yoast SEO meta for many posts in one call (live).

    items: [{post_id, title?, meta_description?, focus_keyword?, reason?}, ...]
    """
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ConnectorError("`items` must be a non-empty array of {post_id, ...}.")
    return await _call(conn, "POST", "/seo/bulk", json={"items": items})


async def get_seo_score(conn: Connection, db, args: dict) -> dict:
    """Return a post's Yoast SEO + readability score and improvement suggestions."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/seo-score")


# ============================================================
# Pages
# ============================================================

async def list_pages(conn: Connection, db, args: dict) -> dict:
    """List all pages with status, URL and which one is the homepage."""
    return await _call(conn, "GET", "/pages")


async def set_homepage(conn: Connection, db, args: dict) -> dict:
    """Set the front page: a static page (page_id) or the latest-posts feed (show_latest_posts=true)."""
    body = _pick(args, "page_id", "posts_page_id", "show_latest_posts", "reason")
    if not body.get("page_id") and not body.get("show_latest_posts"):
        raise ConnectorError("Provide `page_id` (a page) or `show_latest_posts=true`.")
    return await _call(conn, "POST", "/pages/homepage", json=body)


# ============================================================
# Menus — build from scratch
# ============================================================

async def create_menu(conn: Connection, db, args: dict) -> dict:
    """Create a navigation menu (live). Optionally seed items:[{title,url}] and assign a location."""
    if not (args.get("name") or "").strip():
        raise ConnectorError("`name` is required.")
    return await _call(conn, "POST", "/menus/create", json=_pick(args, "name", "items", "location", "reason"))


async def delete_menu(conn: Connection, db, args: dict) -> dict:
    """Delete a navigation menu by id (live)."""
    mid = int(args.get("menu_id") or args.get("id") or 0)
    if not mid:
        raise ConnectorError("`menu_id` is required.")
    return await _call(conn, "POST", f"/menus/{mid}/delete", json={})


async def list_menu_locations(conn: Connection, db, args: dict) -> dict:
    """List the theme's menu locations (header, footer, …) and which menu is assigned to each."""
    return await _call(conn, "GET", "/menus/locations")


async def assign_menu_location(conn: Connection, db, args: dict) -> dict:
    """Assign a menu to a theme location, e.g. location='primary', menu_id=12 (live)."""
    loc = (args.get("location") or "").strip()
    if not loc:
        raise ConnectorError("`location` is required (see list_menu_locations).")
    return await _call(conn, "POST", "/menus/assign", json=_pick(args, "location", "menu_id", "reason"))


# ============================================================
# Plugins — install / update / delete
# ============================================================

async def install_plugin(conn: Connection, db, args: dict) -> dict:
    """Install a plugin from the WordPress.org repository by slug; set activate=true to enable it (live)."""
    if not (args.get("slug") or "").strip():
        raise ConnectorError("`slug` (the WordPress.org plugin slug) is required.")
    return await _call(conn, "POST", "/plugins/install", json=_pick(args, "slug", "activate", "reason"))


async def update_plugin(conn: Connection, db, args: dict) -> dict:
    """Update one plugin to its latest version by file path (live)."""
    if not (args.get("plugin") or "").strip():
        raise ConnectorError("`plugin` (file path) is required.")
    return await _call(conn, "POST", "/plugins/update", json=_pick(args, "plugin", "reason"))


async def update_all_plugins(conn: Connection, db, args: dict) -> dict:
    """Update every plugin that has an available update (live)."""
    return await _call(conn, "POST", "/plugins/update-all", json={})


async def delete_plugin(conn: Connection, db, args: dict) -> dict:
    """Deactivate (if needed) and permanently delete a plugin by file path (live)."""
    if not (args.get("plugin") or "").strip():
        raise ConnectorError("`plugin` (file path) is required.")
    return await _call(conn, "POST", "/plugins/delete", json=_pick(args, "plugin", "reason"))


async def check_updates(conn: Connection, db, args: dict) -> dict:
    """Report all available core, plugin and theme updates."""
    return await _call(conn, "GET", "/updates")


# ============================================================
# Themes — install / update / delete / customize / files / child
# ============================================================

async def install_theme(conn: Connection, db, args: dict) -> dict:
    """Install a theme from the WordPress.org repository by slug; set activate=true to switch to it (live)."""
    if not (args.get("slug") or "").strip():
        raise ConnectorError("`slug` (the WordPress.org theme slug) is required.")
    return await _call(conn, "POST", "/themes/install", json=_pick(args, "slug", "activate", "reason"))


async def update_theme(conn: Connection, db, args: dict) -> dict:
    """Update a theme to its latest version by stylesheet slug (live)."""
    if not (args.get("stylesheet") or "").strip():
        raise ConnectorError("`stylesheet` (theme slug) is required.")
    return await _call(conn, "POST", "/themes/update", json=_pick(args, "stylesheet", "reason"))


async def delete_theme(conn: Connection, db, args: dict) -> dict:
    """Delete an inactive theme by stylesheet slug (live)."""
    if not (args.get("stylesheet") or "").strip():
        raise ConnectorError("`stylesheet` (theme slug) is required.")
    return await _call(conn, "POST", "/themes/delete", json=_pick(args, "stylesheet", "reason"))


async def customize_theme(conn: Connection, db, args: dict) -> dict:
    """Set logo/site-icon (from URL), colors, or arbitrary theme mods (live)."""
    body = _pick(args, "logo_url", "site_icon_url", "background_color", "header_textcolor", "mods", "reason")
    if not body or set(body.keys()) <= {"reason"}:
        raise ConnectorError("Provide at least one of: logo_url, site_icon_url, background_color, header_textcolor, mods.")
    return await _call(conn, "POST", "/themes/customize", json=body)


async def get_theme_file(conn: Connection, db, args: dict) -> dict:
    """Read a theme file's contents, or list editable files when `file` is omitted."""
    params = _pick(args, "stylesheet", "file")
    return await _call(conn, "GET", "/themes/file", params=params or None)



# ============================================================
# Security
# ============================================================

async def security_scan(conn: Connection, db, args: dict) -> dict:
    """Run a security audit: outdated core/plugins/themes, hardening gaps, SSL, admin users → score + issues."""
    return await _call(conn, "GET", "/security/scan")


async def core_integrity_check(conn: Connection, db, args: dict) -> dict:
    """Compare core WordPress files against official WordPress.org checksums; report modified/missing files."""
    return await _call(conn, "GET", "/security/integrity")


async def audit_users(conn: Connection, db, args: dict) -> dict:
    """Audit user accounts: list roles, flag admins and the default 'admin' username, with warnings."""
    return await _call(conn, "GET", "/security/users")


async def harden_site(conn: Connection, db, args: dict) -> dict:
    """Apply security hardening via a must-use plugin (live). Pass all=true, or individual flags.

    Flags: disable_file_edit, disable_xmlrpc, hide_wp_version, security_headers,
    hide_login_errors, block_user_enumeration. Re-run with flags false to relax.
    """
    return await _call(conn, "POST", "/security/harden", json=_pick(
        args, "all", "disable_file_edit", "disable_xmlrpc", "hide_wp_version",
        "security_headers", "hide_login_errors", "block_user_enumeration", "reason"))


async def malware_scan(conn: Connection, db, args: dict) -> dict:
    """Heuristically scan uploads/themes/plugins for suspicious PHP (eval, base64, shells) and PHP in /uploads."""
    return await _call(conn, "GET", "/security/malware")


async def ssl_check(conn: Connection, db, args: dict) -> dict:
    """Check HTTPS configuration: site/home URL scheme, current request SSL, FORCE_SSL_ADMIN."""
    return await _call(conn, "GET", "/security/ssl")


# ============================================================
# WooCommerce
# ============================================================

async def wc_list_products(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce products (name, price, stock, status)."""
    params = {"per_page": max(1, min(int(args.get("per_page") or 20), 100)), "page": max(1, int(args.get("page") or 1))}
    if (args.get("search") or "").strip():
        params["search"] = args["search"].strip()
    return await _call(conn, "GET", "/woo/products", params=params)


async def wc_get_product(conn: Connection, db, args: dict) -> dict:
    """Get one WooCommerce product with full details."""
    pid = int(args.get("product_id") or args.get("id") or 0)
    if not pid:
        raise ConnectorError("`product_id` is required.")
    return await _call(conn, "GET", f"/woo/products/{pid}")


async def wc_create_product(conn: Connection, db, args: dict) -> dict:
    """Create a WooCommerce product (live). name required."""
    if not (args.get("name") or "").strip():
        raise ConnectorError("`name` is required.")
    body = _pick(args, "name", "regular_price", "sale_price", "description", "short_description",
                 "sku", "stock_quantity", "status", "categories", "image_url", "reason")
    return await _call(conn, "POST", "/woo/products/create", json=body)


async def wc_update_product(conn: Connection, db, args: dict) -> dict:
    """Update a WooCommerce product — price, stock, status, description, etc. (live)."""
    pid = int(args.get("product_id") or args.get("id") or 0)
    if not pid:
        raise ConnectorError("`product_id` is required.")
    body = _pick(args, "name", "regular_price", "sale_price", "description",
                 "stock_quantity", "stock_status", "status", "reason")
    if not body:
        raise ConnectorError("Provide at least one field to update.")
    return await _call(conn, "POST", f"/woo/products/{pid}/update", json=body)


async def wc_list_orders(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce orders, optionally filtered by status (processing/completed/…)."""
    params = {"per_page": max(1, min(int(args.get("per_page") or 20), 100)), "page": max(1, int(args.get("page") or 1))}
    if (args.get("status") or "").strip():
        params["status"] = args["status"].strip()
    return await _call(conn, "GET", "/woo/orders", params=params)


async def wc_get_order(conn: Connection, db, args: dict) -> dict:
    """Get one WooCommerce order with line items and customer details."""
    oid = int(args.get("order_id") or args.get("id") or 0)
    if not oid:
        raise ConnectorError("`order_id` is required.")
    return await _call(conn, "GET", f"/woo/orders/{oid}")


async def wc_update_order_status(conn: Connection, db, args: dict) -> dict:
    """Change a WooCommerce order's status, e.g. status='completed' (live)."""
    oid = int(args.get("order_id") or args.get("id") or 0)
    status = (args.get("status") or "").strip()
    if not oid or not status:
        raise ConnectorError("`order_id` and `status` are required.")
    return await _call(conn, "POST", f"/woo/orders/{oid}/status", json=_pick(args, "status", "note", "reason"))


async def wc_sales_summary(conn: Connection, db, args: dict) -> dict:
    """WooCommerce sales summary for the last N days (orders, revenue, AOV, items)."""
    params = {"days": max(1, min(int(args.get("days") or 30), 365))}
    return await _call(conn, "GET", "/woo/sales", params=params)


# ============================================================
# Onboarding
# ============================================================

async def test_connection(conn: Connection, db, args: dict) -> dict:
    """Diagnose the connection: plugin reachable? token valid? is the host stripping the auth header?"""
    base, _ = _conf(conn)
    result = {"site_url": base.replace("/wp-json/falcon/v1", "")}
    try:
        # /selftest is deliberately unauthenticated and minimal (just reachability +
        # whether the Authorization header arrived) — no system fingerprinting info.
        self_t = await _call(conn, "GET", "/selftest")
    except ConnectorError as e:
        return {**result, "connected": False, "stage": "reach", "error": str(e),
                "hint": "Is the TechShu SEO Bridge plugin installed & active, pretty permalinks on, and the site URL correct?"}
    result.update({
        "plugin_reachable": True,
        "plugin_version": self_t.get("version"),
        "auth_header_received": self_t.get("auth_header_received"),
    })
    try:
        site = await _call(conn, "GET", "/site")
        result["connected"] = True
        result["message"] = "✅ Connected — token valid."
        # These diagnostics require a valid token, so they come from the authenticated
        # /site response. Fall back to /selftest's copy for older plugin versions.
        for k in ("yoast_active", "woocommerce_active", "elementor_active", "acf_active", "uploads_writable"):
            result[k] = site.get(k, self_t.get(k))
    except ConnectorError as e:
        result["connected"] = False
        result["error"] = str(e)
        if self_t.get("auth_header_received") is False:
            result["message"] = ("❌ Your server is stripping the Authorization header. The plugin auto-adds an "
                                 ".htaccess fix on activation — re-activate the plugin, or ask the host to allow it.")
        else:
            result["message"] = "❌ Token rejected. Re-copy the token from the plugin's TechShu SEO Bridge settings screen."
    return result


# ============================================================
# Content & SEO (advanced)
# ============================================================

async def bulk_find_replace(conn: Connection, db, args: dict) -> dict:
    """Find-and-replace text across all posts/pages (live). Use dry_run=true to preview first."""
    if not (args.get("find") or "").strip():
        raise ConnectorError("`find` is required.")
    body = _pick(args, "find", "replace", "post_types", "include_title", "dry_run", "reason")
    return await _call(conn, "POST", "/content/find-replace", json=body)


async def schedule_post(conn: Connection, db, args: dict) -> dict:
    """Schedule a post to publish at a future time (live). publish_at is an ISO datetime; pass post_id to schedule an existing post or title to create one."""
    if not (args.get("publish_at") or "").strip():
        raise ConnectorError("`publish_at` (ISO datetime, e.g. 2026-07-01T09:00:00) is required.")
    body = _pick(args, "post_id", "title", "content", "type", "publish_at", "reason")
    return await _call(conn, "POST", "/posts/schedule", json=body)


async def content_calendar(conn: Connection, db, args: dict) -> dict:
    """List all scheduled (future) posts and pages with their publish dates."""
    return await _call(conn, "GET", "/posts/scheduled")


async def list_images_missing_alt(conn: Connection, db, args: dict) -> dict:
    """List media-library images that have no alt text (so AI can write it)."""
    params = {"per_page": max(1, min(int(args.get("per_page") or 100), 300))}
    return await _call(conn, "GET", "/media/missing-alt", params=params)


async def set_image_alt(conn: Connection, db, args: dict) -> dict:
    """Set alt text for one image (media_id + alt) or many (items:[{media_id, alt}]) (live)."""
    items = args.get("items")
    if isinstance(items, list) and items:
        return await _call(conn, "POST", "/media/alt", json={"items": items, "reason": args.get("reason", "")})
    if not int(args.get("media_id") or 0):
        raise ConnectorError("Provide `media_id` + `alt`, or `items`:[{media_id, alt}].")
    return await _call(conn, "POST", "/media/alt", json=_pick(args, "media_id", "alt", "reason"))


async def list_redirects(conn: Connection, db, args: dict) -> dict:
    """List all 301/302 redirects BRING-DATA manages on the site."""
    return await _call(conn, "GET", "/redirects")


async def add_redirect(conn: Connection, db, args: dict) -> dict:
    """Add a redirect (live). from = path or full URL, to = destination, type = 301|302|307 (default 301)."""
    if not (args.get("from") or "").strip() or not (args.get("to") or "").strip():
        raise ConnectorError("`from` and `to` are required.")
    return await _call(conn, "POST", "/redirects/add", json=_pick(args, "from", "to", "type", "reason"))


async def delete_redirect(conn: Connection, db, args: dict) -> dict:
    """Delete a redirect by its id (live)."""
    if not (args.get("id") or "").strip():
        raise ConnectorError("`id` is required (from list_redirects).")
    return await _call(conn, "POST", "/redirects/delete", json=_pick(args, "id"))


async def get_robots_txt(conn: Connection, db, args: dict) -> dict:
    """Get the site's robots.txt (custom override if set, plus the public robots URL)."""
    return await _call(conn, "GET", "/robots")


async def update_robots_txt(conn: Connection, db, args: dict) -> dict:
    """Set a custom robots.txt (live). Pass an empty content string to reset to the WordPress default."""
    if args.get("content") is None:
        raise ConnectorError("`content` is required (empty string resets to default).")
    return await _call(conn, "POST", "/robots/update", json=_pick(args, "content", "reason"))


async def get_sitemaps(conn: Connection, db, args: dict) -> dict:
    """Report sitemap URLs (WordPress core and/or Yoast) and whether the site is indexable."""
    return await _call(conn, "GET", "/sitemaps")


async def get_schema(conn: Connection, db, args: dict) -> dict:
    """Get the custom JSON-LD schema attached to a post."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/schema")


async def set_schema(conn: Connection, db, args: dict) -> dict:
    """Attach custom JSON-LD structured data to a post (live). schema = object or JSON string; empty string removes it."""
    pid = _post_id(args)
    if args.get("schema") is None:
        raise ConnectorError("`schema` is required (a JSON-LD object or string).")
    return await _call(conn, "POST", f"/posts/{pid}/schema/set", json=_pick(args, "schema", "reason"))


async def set_social_meta(conn: Connection, db, args: dict) -> dict:
    """Set OpenGraph / Twitter card meta for a post (live). Uses Yoast fields if Yoast is active, else BRING-DATA's own tags."""
    pid = _post_id(args)
    body = _pick(args, "og_title", "og_description", "og_image_url",
                 "twitter_title", "twitter_description", "twitter_image_url", "reason")
    if not body or set(body.keys()) <= {"reason"}:
        raise ConnectorError("Provide at least one of og_title/og_description/og_image_url/twitter_*.")
    return await _call(conn, "POST", f"/posts/{pid}/social", json=body)


# ============================================================
# Structure & builders
# ============================================================

async def build_gutenberg_page(conn: Connection, db, args: dict) -> dict:
    """Build a page/post from a simple block list (live). blocks:[{type,...}] where type is
    heading|paragraph|image|list|button|quote|html. Pass post_id (+append) to add to an existing post,
    or title to create a new one."""
    blocks = args.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ConnectorError("`blocks` must be a non-empty array of {type, ...}.")
    body = _pick(args, "blocks", "post_id", "title", "type", "status", "append", "reason")
    return await _call(conn, "POST", "/blocks/build", json=body)


async def get_elementor_data(conn: Connection, db, args: dict) -> dict:
    """Read a post's raw Elementor layout JSON (requires Elementor active)."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/elementor")


async def set_elementor_data(conn: Connection, db, args: dict) -> dict:
    """Write a post's Elementor layout JSON and flip it into builder mode (live; requires Elementor)."""
    pid = _post_id(args)
    if args.get("elementor_data") is None:
        raise ConnectorError("`elementor_data` (JSON string or array) is required.")
    return await _call(conn, "POST", f"/posts/{pid}/elementor/set", json=_pick(args, "elementor_data", "reason"))


async def build_elementor_page(conn: Connection, db, args: dict) -> dict:
    """Build an Elementor page from a simple block list (live; Elementor free — no raw JSON needed).

    blocks:[{type, ...}] where type is heading|paragraph|button|image|spacer|divider|video|html.
    Per block: heading{text, level h1-h6, align}; paragraph/text{text}; button{text, link, align};
    image{url, align}; spacer{size}; video{url}; html{html}. Any block may set background (hex) + padding (px)
    on its section.

    MULTI-COLUMN rows: use a block {type:"columns", columns:[ ... ]} (alias "row"). Each entry in `columns`
    is either a list of blocks (even widths) or {blocks:[...], width:<percent>} for a custom column width — e.g.
    a 2-up feature row, or text {width:60} beside an image {width:40}. Columns can hold multiple widgets and
    the row itself can take background/padding.

    Pass title to create a new page, or post_id to rebuild an existing one. canvas=true uses the blank Elementor
    Canvas template (no header/footer) — ideal for landing pages. Free widgets only (no Pro).
    """
    blocks = args.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ConnectorError("`blocks` must be a non-empty array of {type, ...}.")
    if not args.get("post_id") and not (args.get("title") or "").strip():
        raise ConnectorError("Provide `title` (to create a new page) or `post_id` (to rebuild an existing one).")
    return await _call(conn, "POST", "/elementor/build", json=_pick(args, "blocks", "post_id", "title", "type", "status", "canvas", "reason"))


async def get_custom_fields(conn: Connection, db, args: dict) -> dict:
    """Read a post's custom fields — ACF fields (if ACF active) and visible post meta."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/fields")


async def set_custom_field(conn: Connection, db, args: dict) -> dict:
    """Set a custom field on a post (live). key + value; set acf=true to write via ACF's update_field."""
    pid = _post_id(args)
    if not (args.get("key") or "").strip():
        raise ConnectorError("`key` is required.")
    return await _call(conn, "POST", f"/posts/{pid}/fields/set", json=_pick(args, "key", "value", "acf", "reason"))


async def list_acf_field_groups(conn: Connection, db, args: dict) -> dict:
    """List ACF field groups defined on the site and the fields in each (requires ACF active)."""
    return await _call(conn, "GET", "/acf/field-groups")


async def create_acf_field_group(conn: Connection, db, args: dict) -> dict:
    """Create an ACF field group — the field DEFINITIONS, not just values (live; requires ACF, works on free).

    title + fields:[{label, name?, type?, required?, instructions?, choices?, default_value?}] (type defaults
    to text; name auto-derived from label). Choose where it shows with post_types:["page","post",...] or pass a
    raw ACF `location` array. The group is imported into the DB so it's visible/editable in WP-admin → ACF and
    its fields can then be read/written with get_custom_fields / set_custom_field(acf=true).
    NOTE: ACF free has no Repeater/Flexible Content/Clone/Gallery field types — those need ACF Pro.
    """
    if not (args.get("title") or "").strip():
        raise ConnectorError("`title` is required.")
    fields = args.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ConnectorError("`fields` must be a non-empty array of {label, type?, name?, ...}.")
    body = _pick(args, "title", "fields", "post_types", "location", "key", "active", "reason")
    return await _call(conn, "POST", "/acf/field-groups/create", json=body)


async def list_widget_areas(conn: Connection, db, args: dict) -> dict:
    """List the theme's widget areas (sidebars) and the widgets currently in each."""
    return await _call(conn, "GET", "/widgets")


async def add_html_widget(conn: Connection, db, args: dict) -> dict:
    """Add a Custom HTML widget to a widget area (live). sidebar_id + content (+ optional title)."""
    if not (args.get("sidebar_id") or "").strip() or args.get("content") is None:
        raise ConnectorError("`sidebar_id` and `content` are required.")
    return await _call(conn, "POST", "/widgets/add", json=_pick(args, "sidebar_id", "content", "title", "reason"))


async def list_post_types(conn: Connection, db, args: dict) -> dict:
    """List all public post types (including custom post types) with counts — use the slug as `type` in list_posts/create_post."""
    return await _call(conn, "GET", "/post-types")


async def list_page_templates(conn: Connection, db, args: dict) -> dict:
    """List page templates on the active theme (plus Elementor Canvas/Full-Width if Elementor is active).

    Pass one returned `slug` as `template` in create_post/update_post to render a page full-width
    without the theme's header/footer/sidebar — ideal for landing pages.
    """
    return await _call(conn, "GET", "/page-templates")


# ============================================================
# Maintenance & safety
# ============================================================

async def create_content_backup(conn: Connection, db, args: dict) -> dict:
    """Snapshot all post/page content to a download-protected JSON backup (live)."""
    return await _call(conn, "POST", "/backup/create", json=_pick(args, "note", "reason"))


async def list_backups(conn: Connection, db, args: dict) -> dict:
    """List content backups BRING-DATA has created (newest first)."""
    return await _call(conn, "GET", "/backup/list")


async def db_status(conn: Connection, db, args: dict) -> dict:
    """Report cleanable database clutter: revisions, auto-drafts, trash, spam, transients."""
    return await _call(conn, "GET", "/db/status")


async def db_cleanup(conn: Connection, db, args: dict) -> dict:
    """Delete database clutter (live). all=true cleans everything, or set individual flags."""
    return await _call(conn, "POST", "/db/cleanup", json=_pick(
        args, "all", "revisions", "auto_drafts", "spam", "trash", "transients", "orphan_meta", "reason"))


async def clear_cache(conn: Connection, db, args: dict) -> dict:
    """Purge caches — detects WP Rocket, LiteSpeed, W3TC, WP Super Cache, SiteGround, Cache Enabler + object cache (live)."""
    return await _call(conn, "POST", "/cache/clear", json={})


async def performance_audit(conn: Connection, db, args: dict) -> dict:
    """Server-side performance signals: active plugins, autoloaded option size, object cache, PHP version + issues."""
    return await _call(conn, "GET", "/performance")


async def scan_broken_links(conn: Connection, db, args: dict) -> dict:
    """Scan post/page content for broken links (4xx/unreachable). Optionally limit to one post_id."""
    params = {"limit": max(1, min(int(args.get("limit") or 50), 100))}
    if int(args.get("post_id") or 0):
        params["post_id"] = int(args["post_id"])
    if int(args.get("scan_posts") or 0):
        params["scan_posts"] = int(args["scan_posts"])
    return await _call(conn, "GET", "/links/broken", params=params)


async def list_forms(conn: Connection, db, args: dict) -> dict:
    """List contact forms detected on the site (Contact Form 7, WPForms, Gravity Forms)."""
    return await _call(conn, "GET", "/forms")


async def get_form_submissions(conn: Connection, db, args: dict) -> dict:
    """Read form submissions/entries (Gravity, WPForms, or CF7-via-Flamingo). Pass form_id where supported."""
    params = {"limit": max(1, min(int(args.get("limit") or 30), 100))}
    if int(args.get("form_id") or 0):
        params["form_id"] = int(args["form_id"])
    return await _call(conn, "GET", "/forms/submissions", params=params)


def _cf7_id(args: dict) -> int:
    fid = args.get("form_id") or args.get("id")
    try:
        return int(fid)
    except (TypeError, ValueError):
        raise ConnectorError("`form_id` is required (integer).")


async def list_cf7_forms(conn: Connection, db, args: dict) -> dict:
    """List all Contact Form 7 forms (id, title, shortcode). Requires CF7 active (free)."""
    return await _call(conn, "GET", "/cf7/forms")


async def get_cf7_form(conn: Connection, db, args: dict) -> dict:
    """Get one CF7 form's full definition — the form markup, mail settings and messages."""
    return await _call(conn, "GET", f"/cf7/forms/{_cf7_id(args)}")


async def create_cf7_form(conn: Connection, db, args: dict) -> dict:
    """Create a Contact Form 7 form (live; free). title required.

    Optional `form` = CF7 form markup using CF7 tags, e.g.
      <label>Your name [text* your-name]</label>
      <label>Email [email* your-email]</label>
      <label>Message [textarea your-message]</label>
      [submit "Send"]
    (a default name/email/subject/message form is used if omitted). Optional `mail` object overrides the
    notification email: {subject, sender, recipient, body, additional_headers, use_html}. Returns the shortcode.
    """
    if not (args.get("title") or "").strip():
        raise ConnectorError("`title` is required.")
    return await _call(conn, "POST", "/cf7/forms/create", json=_pick(args, "title", "form", "mail", "mail_2", "messages", "reason"))


async def update_cf7_form(conn: Connection, db, args: dict) -> dict:
    """Update a CF7 form (live). Any of title / form (markup) / mail / mail_2 / messages."""
    fid = _cf7_id(args)
    body = _pick(args, "title", "form", "mail", "mail_2", "messages", "reason")
    if not body or set(body.keys()) <= {"reason"}:
        raise ConnectorError("Provide at least one of: title, form, mail, mail_2, messages.")
    return await _call(conn, "POST", f"/cf7/forms/{fid}/update", json=body)


async def delete_cf7_form(conn: Connection, db, args: dict) -> dict:
    """Permanently delete a Contact Form 7 form (live)."""
    fid = _cf7_id(args)
    return await _call(conn, "POST", f"/cf7/forms/{fid}/delete", json=_pick(args, "reason"))


# ============================================================
# Themes (advanced / FSE)
# ============================================================

async def list_block_templates(conn: Connection, db, args: dict) -> dict:
    """List Full-Site-Editing block templates or template parts (block themes, WP 5.9+). type=wp_template|wp_template_part."""
    params = {"type": (args.get("type") or "wp_template").strip()}
    return await _call(conn, "GET", "/templates", params=params)


async def get_block_template(conn: Connection, db, args: dict) -> dict:
    """Get one FSE block template's content by id (e.g. 'theme//index')."""
    if not (args.get("id") or "").strip():
        raise ConnectorError("`id` is required (e.g. 'mytheme//index').")
    params = {"id": args["id"].strip(), "type": (args.get("type") or "wp_template").strip()}
    return await _call(conn, "GET", "/templates/get", params=params)


async def edit_block_template(conn: Connection, db, args: dict) -> dict:
    """Create or overwrite an FSE block template/part for the active theme (live). slug + content; type=wp_template|wp_template_part."""
    if not (args.get("slug") or "").strip() or args.get("content") is None:
        raise ConnectorError("`slug` and `content` are required.")
    return await _call(conn, "POST", "/templates/edit", json=_pick(args, "slug", "content", "type", "reason"))


async def get_global_styles(conn: Connection, db, args: dict) -> dict:
    """Read the theme.json user global styles (colors, typography, spacing) of a block theme."""
    return await _call(conn, "GET", "/global-styles")


async def set_global_styles(conn: Connection, db, args: dict) -> dict:
    """Update theme.json global styles (live; block themes). global_styles = object/JSON; merge=true deep-merges with current."""
    if args.get("global_styles") is None:
        raise ConnectorError("`global_styles` (object or JSON string) is required.")
    return await _call(conn, "POST", "/global-styles/set", json=_pick(args, "global_styles", "merge", "reason"))


# ============================================================
# WooCommerce (bulk & more)
# ============================================================

async def wc_bulk_update_products(conn: Connection, db, args: dict) -> dict:
    """Bulk-update many products (live). Target by ids:[] or filter:{category,status,stock_status}; set:{regular_price,sale_price,stock_quantity,stock_status,status,category} and/or price_adjust:{type:percent|fixed,value,field:regular|sale}."""
    body = _pick(args, "ids", "filter", "set", "price_adjust", "limit", "reason")
    if not body.get("set") and not body.get("price_adjust"):
        raise ConnectorError("Provide `set` and/or `price_adjust`.")
    return await _call(conn, "POST", "/woo/products/bulk-update", json=body)


async def wc_bulk_create_products(conn: Connection, db, args: dict) -> dict:
    """Create many products at once (live). products:[{name, regular_price, ...}]."""
    if not isinstance(args.get("products"), list) or not args["products"]:
        raise ConnectorError("`products` must be a non-empty array.")
    return await _call(conn, "POST", "/woo/products/bulk-create", json=_pick(args, "products", "reason"))


async def wc_export_products(conn: Connection, db, args: dict) -> dict:
    """Export all products to a download-protected CSV; returns the file URL."""
    return await _call(conn, "GET", "/woo/products/export")


async def wc_import_products(conn: Connection, db, args: dict) -> dict:
    """Import/upsert products by SKU from a CSV URL or rows:[{...}] (live)."""
    if not (args.get("csv_url") or "").strip() and not isinstance(args.get("rows"), list):
        raise ConnectorError("Provide `csv_url` or `rows`.")
    return await _call(conn, "POST", "/woo/products/import", json=_pick(args, "csv_url", "rows", "reason"))


async def wc_bulk_order_status(conn: Connection, db, args: dict) -> dict:
    """Set the status of many orders at once (live). order_ids:[] + status (e.g. completed)."""
    if not isinstance(args.get("order_ids"), list) or not args["order_ids"]:
        raise ConnectorError("`order_ids` must be a non-empty array.")
    if not (args.get("status") or "").strip():
        raise ConnectorError("`status` is required.")
    return await _call(conn, "POST", "/woo/orders/bulk-status", json=_pick(args, "order_ids", "status", "note", "reason"))


async def wc_list_coupons(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce coupons (code, type, amount, expiry, usage)."""
    return await _call(conn, "GET", "/woo/coupons")


async def wc_create_coupon(conn: Connection, db, args: dict) -> dict:
    """Create a WooCommerce coupon (live). code required; discount_type=percent|fixed_cart|fixed_product."""
    if not (args.get("code") or "").strip():
        raise ConnectorError("`code` is required.")
    return await _call(conn, "POST", "/woo/coupons/create", json=_pick(
        args, "code", "discount_type", "amount", "expires", "minimum_amount", "usage_limit", "free_shipping", "reason"))


async def wc_delete_coupon(conn: Connection, db, args: dict) -> dict:
    """Delete a WooCommerce coupon by id (live)."""
    if not int(args.get("coupon_id") or 0):
        raise ConnectorError("`coupon_id` is required.")
    return await _call(conn, "POST", "/woo/coupons/delete", json=_pick(args, "coupon_id", "reason"))


async def wc_list_product_categories(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce product categories."""
    return await _call(conn, "GET", "/woo/categories")


async def wc_create_product_category(conn: Connection, db, args: dict) -> dict:
    """Create a WooCommerce product category (live)."""
    if not (args.get("name") or "").strip():
        raise ConnectorError("`name` is required.")
    return await _call(conn, "POST", "/woo/categories/create", json=_pick(args, "name", "parent", "description", "reason"))


async def wc_low_stock(conn: Connection, db, args: dict) -> dict:
    """List products at or below a stock threshold (default 5)."""
    params = {"threshold": int(args.get("threshold") or 5)}
    return await _call(conn, "GET", "/woo/low-stock", params=params)


async def wc_top_sellers(conn: Connection, db, args: dict) -> dict:
    """List best-selling products by total sales (default top 10)."""
    params = {"limit": max(1, min(int(args.get("limit") or 10), 50))}
    return await _call(conn, "GET", "/woo/top-sellers", params=params)


# ============================================================
# Images
# ============================================================

async def image_capabilities(conn: Connection, db, args: dict) -> dict:
    """Report server image support (GD, Imagick, WebP) before running image operations."""
    return await _call(conn, "GET", "/images/capabilities")


async def convert_to_webp(conn: Connection, db, args: dict) -> dict:
    """Convert media images to WebP (live). One media_id or a batch (limit). replace=true swaps the attachment to the WebP file. Needs server WebP support."""
    return await _call(conn, "POST", "/images/webp", json=_pick(args, "media_id", "limit", "quality", "replace", "reason"))


async def optimize_images(conn: Connection, db, args: dict) -> dict:
    """Recompress media images at a target quality to shrink file size (live). One media_id or a batch (limit)."""
    return await _call(conn, "POST", "/images/optimize", json=_pick(args, "media_id", "limit", "quality", "reason"))


async def resize_image(conn: Connection, db, args: dict) -> dict:
    """Resize an image's original file to max_width/max_height (live). crop=true to hard-crop."""
    if not int(args.get("media_id") or 0):
        raise ConnectorError("`media_id` is required.")
    if not int(args.get("max_width") or 0) and not int(args.get("max_height") or 0):
        raise ConnectorError("Provide `max_width` and/or `max_height`.")
    return await _call(conn, "POST", "/images/resize", json=_pick(args, "media_id", "max_width", "max_height", "crop", "reason"))


async def regenerate_thumbnails(conn: Connection, db, args: dict) -> dict:
    """Regenerate all thumbnail sizes for one image (media_id) or a batch — run after a theme change (live)."""
    return await _call(conn, "POST", "/images/regenerate", json=_pick(args, "media_id", "limit", "reason"))


async def enable_lazy_load(conn: Connection, db, args: dict) -> dict:
    """Toggle image lazy-loading site-wide (live). enabled defaults to true."""
    return await _call(conn, "POST", "/images/lazyload", json=_pick(args, "enabled", "reason"))


# ============================================================
# SEO power
# ============================================================

async def internal_link_audit(conn: Connection, db, args: dict) -> dict:
    """Sitewide internal-link audit: orphan pages (no inbound internal links) + most-linked pages."""
    return await _call(conn, "GET", "/seo/internal-links")


async def content_audit(conn: Connection, db, args: dict) -> dict:
    """Bulk content audit: thin content, missing meta description / focus keyword / featured image, duplicate titles."""
    params = {"limit": max(1, min(int(args.get("limit") or 500), 1000)), "type": (args.get("type") or "post").strip()}
    return await _call(conn, "GET", "/seo/content-audit", params=params)


async def get_404_log(conn: Connection, db, args: dict) -> dict:
    """List logged 404 hits (URL, count, referrer) — feed these into the redirect manager."""
    return await _call(conn, "GET", "/seo/404-log")


async def clear_404_log(conn: Connection, db, args: dict) -> dict:
    """Clear the 404 log (live)."""
    return await _call(conn, "POST", "/seo/404-log/clear", json={})


async def apply_schema_template(conn: Connection, db, args: dict) -> dict:
    """Generate & attach JSON-LD from a template (live). template = Article|Product|FAQPage|LocalBusiness|BreadcrumbList; pass data for the dynamic parts."""
    pid = _post_id(args)
    if not (args.get("template") or "").strip():
        raise ConnectorError("`template` is required (Article|Product|FAQPage|LocalBusiness|BreadcrumbList).")
    return await _call(conn, "POST", f"/posts/{pid}/schema/template", json=_pick(args, "template", "data", "reason"))


async def get_hreflang(conn: Connection, db, args: dict) -> dict:
    """Get the hreflang alternate-language links set on a post."""
    pid = _post_id(args)
    return await _call(conn, "GET", f"/posts/{pid}/hreflang")


async def set_hreflang(conn: Connection, db, args: dict) -> dict:
    """Set hreflang alternate-language links on a post (live). alternates:[{lang, url}]; empty array removes them."""
    pid = _post_id(args)
    if not isinstance(args.get("alternates"), list):
        raise ConnectorError("`alternates` must be an array of {lang, url} (empty array removes them).")
    return await _call(conn, "POST", f"/posts/{pid}/hreflang/set", json=_pick(args, "alternates", "reason"))


# ============================================================
# v1.6 — content, health, comms, Woo reviews/refunds, Google Indexing
# ============================================================

async def find_stale_content(conn: Connection, db, args: dict) -> dict:
    """Find decaying content — published posts not updated in N days (oldest first), with word counts."""
    params = {"days": int(args.get("days") or 180), "limit": max(1, min(int(args.get("limit") or 20), 100)),
              "type": (args.get("type") or "post").strip()}
    return await _call(conn, "GET", "/content/stale", params=params)


async def restore_revision(conn: Connection, db, args: dict) -> dict:
    """Restore a post to one of its revisions (live). post_id + revision_id (from get_post_revisions)."""
    pid = _post_id(args)
    if not int(args.get("revision_id") or 0):
        raise ConnectorError("`revision_id` is required (see get_post_revisions).")
    return await _call(conn, "POST", f"/posts/{pid}/revisions/restore", json=_pick(args, "revision_id", "reason"))


async def list_reusable_blocks(conn: Connection, db, args: dict) -> dict:
    """List reusable blocks (wp_block) you can drop into any post."""
    return await _call(conn, "GET", "/reusable-blocks")


async def create_reusable_block(conn: Connection, db, args: dict) -> dict:
    """Create a reusable block (live). title + content (block markup)."""
    if not (args.get("title") or "").strip() or args.get("content") is None:
        raise ConnectorError("`title` and `content` are required.")
    return await _call(conn, "POST", "/reusable-blocks/create", json=_pick(args, "title", "content", "reason"))


async def list_taxonomies(conn: Connection, db, args: dict) -> dict:
    """List public taxonomies (categories, tags, and any custom ones) with their post types."""
    return await _call(conn, "GET", "/taxonomies")


async def create_term(conn: Connection, db, args: dict) -> dict:
    """Create a term in any taxonomy (live). taxonomy + name; optional parent, description."""
    if not (args.get("taxonomy") or "").strip() or not (args.get("name") or "").strip():
        raise ConnectorError("`taxonomy` and `name` are required.")
    return await _call(conn, "POST", "/taxonomies/term", json=_pick(args, "taxonomy", "name", "parent", "description", "reason"))


async def assign_terms(conn: Connection, db, args: dict) -> dict:
    """Assign taxonomy terms to a post (live). post_id + taxonomy + terms (names/ids); append=true to add."""
    pid = _post_id(args)
    if not (args.get("taxonomy") or "").strip() or args.get("terms") is None:
        raise ConnectorError("`taxonomy` and `terms` are required.")
    return await _call(conn, "POST", f"/posts/{pid}/terms", json=_pick(args, "taxonomy", "terms", "append", "reason"))


async def site_health(conn: Connection, db, args: dict) -> dict:
    """WordPress Site Health: versions (WP/PHP/MySQL), HTTPS, debug mode, object cache, updates, cron."""
    return await _call(conn, "GET", "/site-health")


async def accessibility_audit(conn: Connection, db, args: dict) -> dict:
    """Heuristic accessibility scan: images missing alt, vague link text, site language set."""
    params = {"limit": max(1, min(int(args.get("limit") or 50), 200))}
    return await _call(conn, "GET", "/accessibility", params=params)


async def configure_smtp(conn: Connection, db, args: dict) -> dict:
    """Configure SMTP email delivery (live). host, username, password required; port, encryption, from_email, from_name."""
    for k in ("host", "username", "password"):
        if not (args.get(k) or "").strip():
            raise ConnectorError(f"`{k}` is required.")
    return await _call(conn, "POST", "/smtp/configure", json=_pick(
        args, "host", "port", "username", "password", "encryption", "from_email", "from_name", "reason"))


async def send_test_email(conn: Connection, db, args: dict) -> dict:
    """Send a test email to confirm delivery works (live). Optional `to` (defaults to the admin email)."""
    return await _call(conn, "POST", "/smtp/test", json=_pick(args, "to"))


async def purge_spam(conn: Connection, db, args: dict) -> dict:
    """Permanently delete all spam comments (live)."""
    return await _call(conn, "POST", "/comments/purge-spam", json={})


async def export_wxr(conn: Connection, db, args: dict) -> dict:
    """Export all content to a WordPress WXR (.xml) file; returns a download-protected URL."""
    return await _call(conn, "GET", "/export/wxr")


async def wc_list_reviews(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce product reviews, filtered by status (pending/approved/spam/all)."""
    params = {"status": (args.get("status") or "all").strip(), "per_page": max(1, min(int(args.get("per_page") or 30), 100))}
    return await _call(conn, "GET", "/woo/reviews", params=params)


async def wc_moderate_review(conn: Connection, db, args: dict) -> dict:
    """Moderate a WooCommerce review (live). review_id + status (approve|hold|spam|trash)."""
    rid = int(args.get("review_id") or args.get("id") or 0)
    if not rid or not (args.get("status") or "").strip():
        raise ConnectorError("`review_id` and `status` are required.")
    return await _call(conn, "POST", f"/woo/reviews/{rid}/moderate", json=_pick(args, "status"))


async def wc_refund_order(conn: Connection, db, args: dict) -> dict:
    """Refund a WooCommerce order (live). order_id; amount (defaults to full remaining), reason, restock, refund_payment."""
    oid = int(args.get("order_id") or args.get("id") or 0)
    if not oid:
        raise ConnectorError("`order_id` is required.")
    return await _call(conn, "POST", f"/woo/orders/{oid}/refund", json=_pick(args, "amount", "reason", "restock", "refund_payment"))


async def set_google_service_account(conn: Connection, db, args: dict) -> dict:
    """Store the Google service-account JSON used for the Indexing API (live). Pass the full JSON as service_account_json."""
    if args.get("service_account_json") is None:
        raise ConnectorError("`service_account_json` (the full service-account JSON) is required.")
    return await _call(conn, "POST", "/google/service-account", json=_pick(args, "service_account_json"))


async def index_url(conn: Connection, db, args: dict) -> dict:
    """Submit a URL to Google's Indexing API (live). url; type=URL_UPDATED (default) or URL_DELETED. Needs the service account set + Indexing API enabled + SA as a Search Console owner."""
    if not (args.get("url") or "").strip():
        raise ConnectorError("`url` is required.")
    return await _call(conn, "POST", "/google/index", json=_pick(args, "url", "type", "reason"))


async def index_status(conn: Connection, db, args: dict) -> dict:
    """Check a URL's last Google indexing notification status."""
    if not (args.get("url") or "").strip():
        raise ConnectorError("`url` is required.")
    return await _call(conn, "GET", "/google/index-status", params=_pick(args, "url"))


async def pagespeed(conn: Connection, db, args: dict) -> dict:
    """Real Core Web Vitals via Google PageSpeed Insights (Lighthouse). url + strategy (mobile|desktop).

    Runs against any public URL — doesn't go through the WordPress plugin. Optional api_key in args raises the quota.
    """
    url = (args.get("url") or "").strip()
    if not url:
        raise ConnectorError("`url` is required (a public page URL).")
    params = {
        "url": url,
        "strategy": (args.get("strategy") or "mobile").strip(),
        "category": "performance",
    }
    if (args.get("api_key") or "").strip():
        params["key"] = args["api_key"].strip()
    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    try:
        async with limit_for(endpoint):
            res = await http_request("GET", endpoint, params=params)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code == 429:
        raise ConnectorError("PageSpeed quota hit (429). Pass an `api_key` (free from Google Cloud) for more headroom.")
    if res.status_code >= 400:
        raise ConnectorError(f"PageSpeed error {res.status_code}: {res.text[:300]}")
    try:
        data = res.json()
    except ValueError:
        raise ConnectorError("PageSpeed returned non-JSON.")
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})

    def metric(key):
        a = audits.get(key) or {}
        return a.get("displayValue") or a.get("numericValue")

    perf = (lh.get("categories", {}).get("performance", {}) or {}).get("score")
    return {
        "url": url,
        "strategy": params["strategy"],
        "performance_score": round(perf * 100) if isinstance(perf, (int, float)) else None,
        "lcp": metric("largest-contentful-paint"),
        "cls": metric("cumulative-layout-shift"),
        "inp": metric("interaction-to-next-paint") or metric("experimental-interaction-to-next-paint"),
        "fcp": metric("first-contentful-paint"),
        "tbt": metric("total-blocking-time"),
        "speed_index": metric("speed-index"),
    }


# ============================================================
# Power tools (v1.11)
# ============================================================

async def rest_request(conn: Connection, db, args: dict) -> dict:
    """Call ANY WordPress REST route through the site (live escape hatch). Runs as an admin, so it reaches
    any plugin's REST API. method (GET|POST|PUT|PATCH|DELETE), path (e.g. '/wp/v2/posts', '/wc/v3/products'),
    optional params (query) and body (JSON)."""
    method = (args.get("method") or "GET").upper()
    path = (args.get("path") or "").strip()
    if not path.startswith("/"):
        raise ConnectorError("`path` must start with '/' (e.g. '/wp/v2/posts').")
    body = {"method": method, "path": path}
    if isinstance(args.get("params"), dict):
        body["params"] = args["params"]
    if isinstance(args.get("body"), dict):
        body["body"] = args["body"]
    return await _call(conn, "POST", "/rest/proxy", json=body)


async def db_query(conn: Connection, db, args: dict) -> dict:
    """Run a READ-ONLY SQL query against the WordPress database (live). Only a single SELECT/SHOW/DESCRIBE/
    EXPLAIN/WITH statement is allowed — write/DDL keywords are rejected server-side. Great for custom reports."""
    sql = (args.get("sql") or "").strip()
    if not sql:
        raise ConnectorError("`sql` is required (a single read-only query).")
    return await _call(conn, "POST", "/db/query", json={"sql": sql})


async def get_option(conn: Connection, db, args: dict) -> dict:
    """Read any wp_options value by key."""
    key = (args.get("key") or "").strip()
    if not key:
        raise ConnectorError("`key` is required.")
    return await _call(conn, "GET", "/options/get", params={"key": key})


async def set_option(conn: Connection, db, args: dict) -> dict:
    """Set any wp_options value (live). key + value (string/number/bool/array/object)."""
    key = (args.get("key") or "").strip()
    if not key:
        raise ConnectorError("`key` is required.")
    if "value" not in args:
        raise ConnectorError("`value` is required.")
    return await _call(conn, "POST", "/options/set", json=_pick(args, "key", "value", "reason"))


async def duplicate_post(conn: Connection, db, args: dict) -> dict:
    """Duplicate a post/page as a new draft (copies content, taxonomies and meta) (live). post_id; optional title."""
    pid = _post_id(args)
    return await _call(conn, "POST", f"/posts/{pid}/duplicate", json=_pick(args, "title", "reason"))


async def bulk_delete_posts(conn: Connection, db, args: dict) -> dict:
    """Bulk trash/delete posts (live). Target by ids:[] (safest) or by filter (type/status) with confirm=true.
    force=true permanently deletes; max caps a filter delete (default 100)."""
    body = _pick(args, "ids", "type", "status", "max", "confirm", "force", "reason")
    if not (isinstance(args.get("ids"), list) and args["ids"]) and not args.get("confirm"):
        if not (args.get("type") or args.get("status")):
            raise ConnectorError("Provide `ids`:[] or a filter (type/status) with confirm=true.")
    return await _call(conn, "POST", "/posts/bulk-delete", json=body)


async def list_cron(conn: Connection, db, args: dict) -> dict:
    """List scheduled WP-Cron events (hook, next run, schedule)."""
    return await _call(conn, "GET", "/cron")


async def run_cron(conn: Connection, db, args: dict) -> dict:
    """Run a scheduled cron hook now (live). hook = the action name from list_cron."""
    if not (args.get("hook") or "").strip():
        raise ConnectorError("`hook` is required.")
    return await _call(conn, "POST", "/cron/run", json=_pick(args, "hook", "reason"))


async def clear_cron(conn: Connection, db, args: dict) -> dict:
    """Unschedule all events for a cron hook (live). hook required."""
    if not (args.get("hook") or "").strip():
        raise ConnectorError("`hook` is required.")
    return await _call(conn, "POST", "/cron/clear", json=_pick(args, "hook", "reason"))


async def maintenance_mode(conn: Connection, db, args: dict) -> dict:
    """Get or set front-end maintenance mode. Pass enabled (true/false) to set it (+ optional message);
    omit enabled to just read the current status. Logged-in users always bypass it."""
    if "enabled" in args:
        return await _call(conn, "POST", "/maintenance", json=_pick(args, "enabled", "message", "reason"))
    return await _call(conn, "GET", "/maintenance")


async def reply_to_comment(conn: Connection, db, args: dict) -> dict:
    """Post an approved reply to a comment (live). comment_id + content; optional author, author_email."""
    cid = int(args.get("comment_id") or args.get("id") or 0)
    if not cid:
        raise ConnectorError("`comment_id` is required.")
    if not (args.get("content") or "").strip():
        raise ConnectorError("`content` is required.")
    return await _call(conn, "POST", f"/comments/{cid}/reply", json=_pick(args, "content", "author", "author_email", "reason"))


async def bulk_comment_action(conn: Connection, db, args: dict) -> dict:
    """Apply an action to many comments at once (live). ids:[] + action (approve|unapprove|spam|trash|delete)."""
    if not isinstance(args.get("ids"), list) or not args["ids"]:
        raise ConnectorError("`ids` (array of comment ids) is required.")
    if not (args.get("action") or "").strip():
        raise ConnectorError("`action` is required (approve|unapprove|spam|trash|delete).")
    return await _call(conn, "POST", "/comments/bulk", json=_pick(args, "ids", "action", "reason"))


async def media_replace(conn: Connection, db, args: dict) -> dict:
    """Replace a media file in place from a URL — keeps the same attachment id and URL (live). media_id + url."""
    mid = int(args.get("media_id") or args.get("id") or 0)
    if not mid:
        raise ConnectorError("`media_id` is required.")
    if not (args.get("url") or "").strip():
        raise ConnectorError("`url` (replacement file URL) is required.")
    return await _call(conn, "POST", f"/media/{mid}/replace", json=_pick(args, "url", "reason"))


async def bulk_set_image_alt(conn: Connection, db, args: dict) -> dict:
    """Apply alt text to many media items at once (live). items:[{id, alt}]. Pair with list_images_missing_alt."""
    if not isinstance(args.get("items"), list) or not args["items"]:
        raise ConnectorError("`items` must be a non-empty array of {id, alt}.")
    return await _call(conn, "POST", "/media/bulk-alt", json=_pick(args, "items", "reason"))


async def list_block_patterns(conn: Connection, db, args: dict) -> dict:
    """List registered Gutenberg block patterns (name, title, categories)."""
    return await _call(conn, "GET", "/block-patterns")


async def insert_block_pattern(conn: Connection, db, args: dict) -> dict:
    """Insert a registered block pattern into a post (live). post_id + pattern (name from list_block_patterns);
    append=true adds it to the end, else replaces the content."""
    pid = _post_id(args)
    if not (args.get("pattern") or "").strip():
        raise ConnectorError("`pattern` (a registered pattern name) is required.")
    return await _call(conn, "POST", f"/posts/{pid}/insert-pattern", json=_pick(args, "pattern", "append", "reason"))


async def list_roles(conn: Connection, db, args: dict) -> dict:
    """List user roles and their capabilities."""
    return await _call(conn, "GET", "/roles")


async def set_role_capabilities(conn: Connection, db, args: dict) -> dict:
    """Add/remove capabilities on a role (live). role (slug) + add:[] and/or remove:[] capability names."""
    if not (args.get("role") or "").strip():
        raise ConnectorError("`role` (slug) is required.")
    if not args.get("add") and not args.get("remove"):
        raise ConnectorError("Provide `add`:[] and/or `remove`:[] capabilities.")
    return await _call(conn, "POST", "/roles/caps", json=_pick(args, "role", "add", "remove", "reason"))


async def create_role(conn: Connection, db, args: dict) -> dict:
    """Create a custom user role (live). slug + name; optional capabilities:[] (defaults to ['read'])."""
    if not (args.get("slug") or "").strip() or not (args.get("name") or "").strip():
        raise ConnectorError("`slug` and `name` are required.")
    return await _call(conn, "POST", "/roles/create", json=_pick(args, "slug", "name", "capabilities", "reason"))


async def flush_rewrite_rules(conn: Connection, db, args: dict) -> dict:
    """Flush WordPress rewrite rules (fixes broken permalinks) (live)."""
    return await _call(conn, "POST", "/rewrite/flush", json={})


async def get_debug_log(conn: Connection, db, args: dict) -> dict:
    """Read the tail of wp-content/debug.log and whether WP_DEBUG is on. lines (default 100, max 500)."""
    params = {"lines": max(1, min(int(args.get("lines") or 100), 500))}
    return await _call(conn, "GET", "/debug/log", params=params)


# --- WooCommerce depth ---

async def wc_list_customers(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce customers with order count and total spent."""
    params = {"per_page": max(1, min(int(args.get("per_page") or 20), 100)), "page": max(1, int(args.get("page") or 1))}
    return await _call(conn, "GET", "/woo/customers", params=params)


async def wc_list_variations(conn: Connection, db, args: dict) -> dict:
    """List a variable product's variations (sku, price, stock, attributes). product_id."""
    pid = int(args.get("product_id") or args.get("id") or 0)
    if not pid:
        raise ConnectorError("`product_id` is required.")
    return await _call(conn, "GET", f"/woo/products/{pid}/variations")


async def wc_update_variation(conn: Connection, db, args: dict) -> dict:
    """Update a product variation (live). variation_id; any of regular_price/sale_price/sku/stock_quantity."""
    vid = int(args.get("variation_id") or args.get("id") or 0)
    if not vid:
        raise ConnectorError("`variation_id` is required.")
    body = _pick(args, "regular_price", "sale_price", "sku", "stock_quantity", "reason")
    if not body or set(body.keys()) <= {"reason"}:
        raise ConnectorError("Provide at least one of: regular_price, sale_price, sku, stock_quantity.")
    return await _call(conn, "POST", f"/woo/variations/{vid}/update", json=body)


async def wc_list_shipping_zones(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce shipping zones, their regions and shipping methods."""
    return await _call(conn, "GET", "/woo/shipping-zones")


async def wc_list_tax_rates(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce tax rates."""
    return await _call(conn, "GET", "/woo/tax-rates")


async def wc_list_webhooks(conn: Connection, db, args: dict) -> dict:
    """List WooCommerce webhooks (topic, delivery URL, status)."""
    return await _call(conn, "GET", "/woo/webhooks")


async def wc_create_webhook(conn: Connection, db, args: dict) -> dict:
    """Create a WooCommerce webhook (live). topic (e.g. order.created, product.updated) + delivery_url; optional name, status."""
    if not (args.get("topic") or "").strip() or not (args.get("delivery_url") or "").strip():
        raise ConnectorError("`topic` and `delivery_url` are required.")
    return await _call(conn, "POST", "/woo/webhooks/create", json=_pick(args, "topic", "delivery_url", "name", "status", "reason"))


# ============================================================
# Security suite (v1.12) — whole-site checks
# ============================================================

def _site_origin(conn: Connection) -> str:
    base, _ = _conf(conn)
    return base.split("/wp-json")[0].rstrip("/")


async def vulnerability_scan(conn: Connection, db, args: dict) -> dict:
    """Scan core + every plugin/theme for OUTDATED versions (the #1 real-world attack vector).
    Returns installed vs latest + which have updates. (CVE-level data needs a WPScan key — not required.)"""
    return await _call(conn, "GET", "/security/vulnerabilities")


async def file_integrity_scan(conn: Connection, db, args: dict) -> dict:
    """Scan wp-content for malware indicators: PHP files in uploads/, recently-modified PHP, world-writable files."""
    return await _call(conn, "GET", "/security/file-integrity")


async def file_permissions_audit(conn: Connection, db, args: dict) -> dict:
    """Audit permissions on wp-config.php, .htaccess, wp-content, uploads and the web root; flag world-writable / loose perms."""
    return await _call(conn, "GET", "/security/permissions")


async def hardening_audit(conn: Connection, db, args: dict) -> dict:
    """Config hardening checks: file-editing disabled, debug off, XML-RPC, open registration + default role,
    default table prefix, unique auth salts, readme.html / version leak."""
    return await _call(conn, "GET", "/security/hardening")


async def security_headers_check(conn: Connection, db, args: dict) -> dict:
    """Check the site's HTTP response for key security headers (HSTS, CSP, X-Frame-Options,
    X-Content-Type-Options, Referrer-Policy, Permissions-Policy). Fetches the homepage directly."""
    origin = _site_origin(conn)
    try:
        async with limit_for(origin):
            res = await http_request("GET", origin + "/")
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    h = {k.lower(): v for k, v in dict(res.headers).items()}
    wanted = {"strict-transport-security": "HSTS", "content-security-policy": "Content-Security-Policy",
              "x-frame-options": "X-Frame-Options", "x-content-type-options": "X-Content-Type-Options",
              "referrer-policy": "Referrer-Policy", "permissions-policy": "Permissions-Policy"}
    present = {label: h[k] for k, label in wanted.items() if k in h}
    missing = [label for k, label in wanted.items() if k not in h]
    return {"url": origin, "present": present, "missing": missing, "server": h.get("server"),
            "x_powered_by": h.get("x-powered-by"),
            "warnings": [f"Missing security header: {m}" for m in missing]}


async def login_security(conn: Connection, db, args: dict) -> dict:
    """Audit admin accounts: guessable usernames (admin/root/test), admin count, active sessions per admin."""
    return await _call(conn, "GET", "/security/logins")


async def force_logout_all(conn: Connection, db, args: dict) -> dict:
    """Destroy ALL active login sessions for every user (live) — forces everyone to re-login (use after a breach)."""
    return await _call(conn, "POST", "/security/force-logout", json=_pick(args, "reason"))


async def suspicious_activity_scan(conn: Connection, db, args: dict) -> dict:
    """Hunt for compromise signs: autoloaded options containing code (base64/eval/<script>), suspicious cron hooks,
    administrator accounts created in the last 30 days."""
    return await _call(conn, "GET", "/security/suspicious")


async def secrets_scan(conn: Connection, db, args: dict) -> dict:
    """Check auth salts are set, unique and not default, and that wp-config.php permissions are locked down."""
    return await _call(conn, "GET", "/security/secrets")


async def mixed_content_scan(conn: Connection, db, args: dict) -> dict:
    """Find published posts that reference insecure http:// URLs on your own (https) domain — mixed content that breaks the padlock."""
    return await _call(conn, "GET", "/security/mixed-content")


async def blocklist_check(conn: Connection, db, args: dict) -> dict:
    """Check whether the site's domain is flagged by Google Safe Browsing (malware/phishing). Pass api_key
    (free Google Safe Browsing key) to run the live check."""
    origin = _site_origin(conn)
    key = (args.get("api_key") or "").strip()
    if not key:
        return {"checked": False, "url": origin,
                "note": "Pass a Google Safe Browsing api_key to run the live blocklist check."}
    endpoint = f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={key}"
    body = {"client": {"clientId": "falcon", "clientVersion": "1.0"},
            "threatInfo": {"threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE"],
                           "platformTypes": ["ANY_PLATFORM"], "threatEntryTypes": ["URL"],
                           "threatEntries": [{"url": origin}]}}
    try:
        async with limit_for(endpoint):
            res = await http_request("POST", endpoint, json=body)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(f"Safe Browsing error {res.status_code}: {res.text[:200]}")
    matches = (res.json() or {}).get("matches") or []
    return {"checked": True, "url": origin, "flagged": bool(matches), "matches": matches,
            "warnings": ["Site is FLAGGED on Google Safe Browsing — investigate immediately!"] if matches else []}


async def full_security_audit(conn: Connection, db, args: dict) -> dict:
    """Run the WHOLE-SITE security audit — every server-side check (vulns, file integrity, permissions, hardening,
    logins, suspicious activity, secrets, mixed content) scored 0–100 with a grade, plus HTTP security headers and
    (with blocklist_api_key) a Google Safe Browsing check. Set auto_fix=true to apply safe hardening afterwards."""
    report = await _call(conn, "GET", "/security/audit")
    issues = report.get("issues") or []
    try:
        report["security_headers"] = await security_headers_check(conn, db, {})
        for w in report["security_headers"].get("warnings", []):
            issues.append({"area": "security_headers", "issue": w})
    except ConnectorError as e:
        report["security_headers"] = {"error": str(e)}
    if args.get("blocklist_api_key"):
        try:
            bl = await blocklist_check(conn, db, {"api_key": args["blocklist_api_key"]})
            report["blocklist"] = bl
            for w in bl.get("warnings", []):
                issues.append({"area": "blocklist", "issue": w})
        except ConnectorError as e:
            report["blocklist"] = {"error": str(e)}
    report["issues"] = issues
    report["issue_count"] = len(issues)
    if args.get("auto_fix"):
        try:
            report["auto_fix"] = await harden_site(conn, db, {"reason": "full_security_audit auto_fix"})
        except ConnectorError as e:
            report["auto_fix"] = {"error": str(e)}
    return report


# ============================================================
# Catalog
# ============================================================

CATALOG = {
    "site_info": {
        "description": "WordPress site + TechShu SEO Bridge plugin status: version, Yoast active?, post/page counts.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "list_posts": {
        "description": "List posts or pages with their current Yoast SEO title, meta description, focus keyword and URL.",
        "input": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Optional text filter."},
                "type": {"type": "string", "description": "post | page (default post)."},
                "per_page": {"type": "integer", "description": "1–100 (default 20)."},
                "page": {"type": "integer", "description": "Page number (default 1)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_post": {
        "description": "Get full content + Yoast meta for a single post/page by id.",
        "input": {
            "type": "object",
            "properties": {"post_id": {"type": "integer", "description": "WordPress post/page id."}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
    },
    "recent_changes": {
        "description": "History of SEO changes BRING-DATA has applied to the site (most recent first).",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "update_seo_meta": {
        "description": "Set a new Yoast SEO title, meta description and/or focus keyword for a post. Applied LIVE immediately.",
        "write": True,
        "input": {
            "type": "object",
            "properties": {
                "post_id": {"type": "integer", "description": "WordPress post/page id."},
                "title": {"type": "string", "description": "New SEO title (Yoast)."},
                "meta_description": {"type": "string", "description": "New meta description (Yoast)."},
                "focus_keyword": {"type": "string", "description": "New focus keyword (Yoast)."},
                "reason": {"type": "string", "description": "Why — shown to the human approver (e.g. 'low CTR in GSC')."},
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
    },
    "insert_internal_link": {
        "description": "Insert an anchor-text internal link into a post. Applied LIVE immediately.",
        "write": True,
        "input": {
            "type": "object",
            "properties": {
                "post_id": {"type": "integer", "description": "WordPress post/page id to add the link into."},
                "anchor_text": {"type": "string", "description": "The visible anchor text."},
                "target_url": {"type": "string", "description": "The URL to link to (usually another post on the site)."},
                "reason": {"type": "string", "description": "Why — shown to the human approver."},
            },
            "required": ["post_id", "anchor_text", "target_url"],
            "additionalProperties": False,
        },
    },

    # --- Content management ---
    "create_post": {
        "description": ("Create a post or page (live). content is HTML; status draft|publish|pending|private. "
                        "For a LANDING PAGE: type='page', full_html=true (keeps your <style>/<script> & inline CSS — "
                        "without it WordPress strips them), and template=a full-width/canvas slug from list_page_templates "
                        "(so it drops the theme header/footer/sidebar)."),
        "write": True,
        "input": {"type": "object", "properties": {
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Post body. HTML allowed; with full_html=true a complete <style>…</style> + markup is preserved verbatim."},
            "excerpt": {"type": "string"},
            "status": {"type": "string", "description": "draft|publish|pending|private (default draft)."},
            "type": {"type": "string", "description": "post|page (default post). Use page for landing pages."},
            "full_html": {"type": "boolean", "description": "Keep raw HTML incl. <style>/<script> and inline CSS (no sanitization). Set true for self-contained landing pages."},
            "template": {"type": "string", "description": "Page template slug from list_page_templates (e.g. elementor_canvas) for a full-width landing page. Omit for the theme default."},
            "slug": {"type": "string", "description": "URL slug (post_name), e.g. 'summer-sale'."},
            "categories": {"type": "array", "items": {"type": "string"}, "description": "Category names (created if missing)."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "featured_image_url": {"type": "string", "description": "URL of an image to import & set as featured."},
            "reason": {"type": "string"},
        }, "required": ["title"], "additionalProperties": False},
    },
    "update_post": {
        "description": ("Edit an existing post/page (live). Any of title/content/excerpt/status/categories/tags/featured_image_url/template/slug. "
                        "Use full_html=true to preserve a self-contained HTML/CSS layout (else <style>/<script> are stripped)."),
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"}, "title": {"type": "string"}, "content": {"type": "string"},
            "excerpt": {"type": "string"}, "status": {"type": "string"},
            "full_html": {"type": "boolean", "description": "Keep raw HTML incl. <style>/<script> & inline CSS (no sanitization)."},
            "template": {"type": "string", "description": "Page template slug from list_page_templates (full-width/canvas)."},
            "slug": {"type": "string", "description": "URL slug (post_name)."},
            "categories": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}},
            "featured_image_url": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["post_id"], "additionalProperties": False},
    },
    "delete_post": {
        "description": "Trash a post/page, or permanently delete with force=true (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"}, "force": {"type": "boolean", "description": "true = permanent delete."},
            "reason": {"type": "string"},
        }, "required": ["post_id"], "additionalProperties": False},
    },
    "get_post_revisions": {
        "description": "List a post's revision history (id, author, date, title).",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },

    # --- Media ---
    "upload_media": {
        "description": "Import an image/file into the Media Library from a URL (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Image/file URL to import."},
            "title": {"type": "string"}, "alt": {"type": "string"}, "post_id": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": ["url"], "additionalProperties": False},
    },
    "list_media": {
        "description": "Browse the Media Library (id, title, url, mime, alt).",
        "input": {"type": "object", "properties": {"per_page": {"type": "integer"}, "page": {"type": "integer"}}, "required": [], "additionalProperties": False},
    },
    "delete_media": {
        "description": "Permanently delete a media item (live).",
        "write": True,
        "input": {"type": "object", "properties": {"media_id": {"type": "integer"}}, "required": ["media_id"], "additionalProperties": False},
    },

    # --- Categories & tags ---
    "list_categories": {"description": "List all post categories.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "create_category": {
        "description": "Create a new category (live).",
        "write": True,
        "input": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "parent": {"type": "integer"}, "slug": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
    },
    "list_tags": {"description": "List all post tags.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "create_tag": {
        "description": "Create a new tag (live).",
        "write": True,
        "input": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
    },

    # --- Menus ---
    "list_menus": {"description": "List registered navigation menus.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "get_menu": {
        "description": "Get the items in a navigation menu.",
        "input": {"type": "object", "properties": {"menu_id": {"type": "integer"}}, "required": ["menu_id"], "additionalProperties": False},
    },
    "update_menu": {
        "description": "Add, remove or reorder menu items (live). add:[{title,url}], remove:[item_id], reorder:[item_id,...].",
        "write": True,
        "input": {"type": "object", "properties": {
            "menu_id": {"type": "integer"},
            "add": {"type": "array", "items": {"type": "object"}},
            "remove": {"type": "array", "items": {"type": "integer"}},
            "reorder": {"type": "array", "items": {"type": "integer"}},
            "reason": {"type": "string"},
        }, "required": ["menu_id"], "additionalProperties": False},
    },

    # --- Plugins & themes ---
    "list_plugins": {"description": "List installed plugins with active/inactive status and file path.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "activate_plugin": {
        "description": "Activate a plugin by file path, e.g. 'akismet/akismet.php' (live).",
        "write": True,
        "input": {"type": "object", "properties": {"plugin": {"type": "string"}, "reason": {"type": "string"}}, "required": ["plugin"], "additionalProperties": False},
    },
    "deactivate_plugin": {
        "description": "Deactivate a plugin by file path (live).",
        "write": True,
        "input": {"type": "object", "properties": {"plugin": {"type": "string"}, "reason": {"type": "string"}}, "required": ["plugin"], "additionalProperties": False},
    },
    "list_themes": {"description": "List installed themes (active one flagged).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "activate_theme": {
        "description": "Switch the active theme by stylesheet slug (live).",
        "write": True,
        "input": {"type": "object", "properties": {"stylesheet": {"type": "string"}, "reason": {"type": "string"}}, "required": ["stylesheet"], "additionalProperties": False},
    },

    # --- Users ---
    "list_users": {"description": "List WordPress users with their roles.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "create_user": {
        "description": "Create a new WordPress user (live). A password is generated if omitted.",
        "write": True,
        "input": {"type": "object", "properties": {
            "username": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"},
            "role": {"type": "string", "description": "subscriber|contributor|author|editor|administrator (default subscriber)."}, "reason": {"type": "string"},
        }, "required": ["username", "email"], "additionalProperties": False},
    },
    "update_user_role": {
        "description": "Change a user's role (live).",
        "write": True,
        "input": {"type": "object", "properties": {"user_id": {"type": "integer"}, "role": {"type": "string"}, "reason": {"type": "string"}}, "required": ["user_id", "role"], "additionalProperties": False},
    },

    # --- Settings ---
    "get_site_settings": {"description": "Read general site settings (title, tagline, timezone, formats, etc.).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "update_site_settings": {
        "description": "Update general site settings (live). Allowed: blogname, blogdescription, admin_email, timezone_string, date_format, time_format, start_of_week, posts_per_page, default_category, show_on_front, page_on_front.",
        "write": True,
        "input": {"type": "object", "properties": {
            "blogname": {"type": "string"}, "blogdescription": {"type": "string"}, "admin_email": {"type": "string"},
            "timezone_string": {"type": "string"}, "date_format": {"type": "string"}, "time_format": {"type": "string"},
            "start_of_week": {"type": "integer"}, "posts_per_page": {"type": "integer"}, "default_category": {"type": "integer"},
            "show_on_front": {"type": "string"}, "page_on_front": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },

    # --- Comments ---
    "list_comments": {
        "description": "List comments, filtered by status (pending|approved|spam|trash|all).",
        "input": {"type": "object", "properties": {"status": {"type": "string"}, "per_page": {"type": "integer"}}, "required": [], "additionalProperties": False},
    },
    "approve_comment": {
        "description": "Approve a comment (live).",
        "write": True,
        "input": {"type": "object", "properties": {"comment_id": {"type": "integer"}}, "required": ["comment_id"], "additionalProperties": False},
    },
    "delete_comment": {
        "description": "Delete a comment — to trash, or permanently with force=true (live).",
        "write": True,
        "input": {"type": "object", "properties": {"comment_id": {"type": "integer"}, "force": {"type": "boolean"}}, "required": ["comment_id"], "additionalProperties": False},
    },

    # --- SEO extended ---
    "bulk_update_seo_meta": {
        "description": "Update Yoast SEO meta for many posts in one call (live). items:[{post_id, title?, meta_description?, focus_keyword?, reason?}].",
        "write": True,
        "input": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}, "required": ["items"], "additionalProperties": False},
    },
    "get_seo_score": {
        "description": "Return a post's Yoast SEO + readability score and concrete improvement suggestions.",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },

    # --- Pages ---
    "list_pages": {"description": "List all pages with status, URL and which one is set as the homepage.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "set_homepage": {
        "description": "Set the front page: a static page (page_id) or the latest-posts feed (show_latest_posts=true). Live.",
        "write": True,
        "input": {"type": "object", "properties": {
            "page_id": {"type": "integer", "description": "Page to use as the static homepage."},
            "posts_page_id": {"type": "integer", "description": "Optional page to show the blog/posts on."},
            "show_latest_posts": {"type": "boolean", "description": "true = use the latest-posts feed instead of a static page."},
            "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },

    # --- Menus (build from scratch) ---
    "create_menu": {
        "description": "Create a navigation menu (live). Optionally seed items:[{title,url}] and assign a theme location.",
        "write": True,
        "input": {"type": "object", "properties": {
            "name": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object"}, "description": "[{title, url}] initial links."},
            "location": {"type": "string", "description": "Theme menu location to assign (e.g. primary)."},
            "reason": {"type": "string"},
        }, "required": ["name"], "additionalProperties": False},
    },
    "delete_menu": {
        "description": "Delete a navigation menu by id (live).",
        "write": True,
        "input": {"type": "object", "properties": {"menu_id": {"type": "integer"}}, "required": ["menu_id"], "additionalProperties": False},
    },
    "list_menu_locations": {"description": "List the theme's menu locations (header/footer/…) and which menu is assigned to each.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "assign_menu_location": {
        "description": "Assign a menu to a theme location, e.g. location='primary', menu_id=12 (live).",
        "write": True,
        "input": {"type": "object", "properties": {"location": {"type": "string"}, "menu_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["location"], "additionalProperties": False},
    },

    # --- Plugins (install/update/delete) ---
    "install_plugin": {
        "description": "Install a plugin from the WordPress.org repository by slug; activate=true to enable it (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "WordPress.org plugin slug, e.g. 'wordfence'."},
            "activate": {"type": "boolean", "description": "Activate immediately after install."},
            "reason": {"type": "string"},
        }, "required": ["slug"], "additionalProperties": False},
    },
    "update_plugin": {
        "description": "Update one plugin to its latest version by file path (live).",
        "write": True,
        "input": {"type": "object", "properties": {"plugin": {"type": "string"}, "reason": {"type": "string"}}, "required": ["plugin"], "additionalProperties": False},
    },
    "update_all_plugins": {
        "description": "Update every plugin that has an available update (live).",
        "write": True,
        "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "delete_plugin": {
        "description": "Deactivate (if needed) and permanently delete a plugin by file path (live).",
        "write": True,
        "input": {"type": "object", "properties": {"plugin": {"type": "string"}, "reason": {"type": "string"}}, "required": ["plugin"], "additionalProperties": False},
    },
    "check_updates": {"description": "Report all available core, plugin and theme updates.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},

    # --- Themes (install/update/delete/customize/files/child) ---
    "install_theme": {
        "description": "Install a theme from the WordPress.org repository by slug; activate=true to switch to it (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "WordPress.org theme slug, e.g. 'astra'."},
            "activate": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": ["slug"], "additionalProperties": False},
    },
    "update_theme": {
        "description": "Update a theme to its latest version by stylesheet slug (live).",
        "write": True,
        "input": {"type": "object", "properties": {"stylesheet": {"type": "string"}, "reason": {"type": "string"}}, "required": ["stylesheet"], "additionalProperties": False},
    },
    "delete_theme": {
        "description": "Delete an inactive theme by stylesheet slug (live). The active theme cannot be deleted.",
        "write": True,
        "input": {"type": "object", "properties": {"stylesheet": {"type": "string"}, "reason": {"type": "string"}}, "required": ["stylesheet"], "additionalProperties": False},
    },
    "customize_theme": {
        "description": "Set logo / site icon (from URL), background/header colors, or arbitrary theme mods (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "logo_url": {"type": "string", "description": "Image URL to import & set as the site logo."},
            "site_icon_url": {"type": "string", "description": "Image URL for the favicon / site icon."},
            "background_color": {"type": "string", "description": "Hex (with or without #)."},
            "header_textcolor": {"type": "string", "description": "Hex (with or without #)."},
            "mods": {"type": "object", "description": "Arbitrary {theme_mod: value} pairs."},
            "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "get_theme_file": {
        "description": "Read a theme file's contents, or list editable files when `file` is omitted.",
        "input": {"type": "object", "properties": {
            "stylesheet": {"type": "string", "description": "Theme slug (default: active theme)."},
            "file": {"type": "string", "description": "Relative path, e.g. 'functions.php'. Omit to list files."},
        }, "required": [], "additionalProperties": False},
    },

    # --- Security ---
    "security_scan": {"description": "Security audit: outdated core/plugins/themes, hardening gaps, SSL, admin users → score (A–D) + issues.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "core_integrity_check": {"description": "Compare core WordPress files against official WordPress.org checksums; report modified/missing files.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "audit_users": {"description": "Audit user accounts: roles, admin count, default 'admin' username, with warnings.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "harden_site": {
        "description": "Apply security hardening via a must-use plugin (live). all=true enables everything; or set individual flags.",
        "write": True,
        "input": {"type": "object", "properties": {
            "all": {"type": "boolean", "description": "Enable all hardening measures."},
            "disable_file_edit": {"type": "boolean", "description": "Disable the built-in theme/plugin code editor."},
            "disable_xmlrpc": {"type": "boolean", "description": "Disable XML-RPC."},
            "hide_wp_version": {"type": "boolean", "description": "Remove the WordPress version from page source."},
            "security_headers": {"type": "boolean", "description": "Send X-Frame-Options, nosniff, Referrer-Policy, etc."},
            "hide_login_errors": {"type": "boolean", "description": "Show a generic login error message."},
            "block_user_enumeration": {"type": "boolean", "description": "Block ?author=N user enumeration."},
            "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "malware_scan": {"description": "Heuristically scan uploads/themes/plugins for suspicious PHP (eval, base64, web shells) and PHP files inside /uploads.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "ssl_check": {"description": "Check HTTPS config: site/home URL scheme, current request SSL, FORCE_SSL_ADMIN, with suggestions.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},

    # --- WooCommerce ---
    "wc_list_products": {
        "description": "List WooCommerce products (name, price, stock, status). Errors if WooCommerce isn't active.",
        "input": {"type": "object", "properties": {"search": {"type": "string"}, "per_page": {"type": "integer"}, "page": {"type": "integer"}}, "required": [], "additionalProperties": False},
    },
    "wc_get_product": {
        "description": "Get one WooCommerce product with full details (description, categories).",
        "input": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"], "additionalProperties": False},
    },
    "wc_create_product": {
        "description": "Create a WooCommerce product (live). name required; set prices, stock, image, categories.",
        "write": True,
        "input": {"type": "object", "properties": {
            "name": {"type": "string"}, "regular_price": {"type": "string"}, "sale_price": {"type": "string"},
            "description": {"type": "string"}, "short_description": {"type": "string"}, "sku": {"type": "string"},
            "stock_quantity": {"type": "integer"}, "status": {"type": "string", "description": "publish|draft|pending|private."},
            "categories": {"type": "array", "items": {"type": "string"}}, "image_url": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["name"], "additionalProperties": False},
    },
    "wc_update_product": {
        "description": "Update a WooCommerce product — price, stock, status, description, etc. (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "product_id": {"type": "integer"}, "name": {"type": "string"}, "regular_price": {"type": "string"},
            "sale_price": {"type": "string"}, "description": {"type": "string"}, "stock_quantity": {"type": "integer"},
            "stock_status": {"type": "string", "description": "instock|outofstock|onbackorder."}, "status": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["product_id"], "additionalProperties": False},
    },
    "wc_list_orders": {
        "description": "List WooCommerce orders, optionally filtered by status (processing|completed|cancelled|refunded|…).",
        "input": {"type": "object", "properties": {"status": {"type": "string"}, "per_page": {"type": "integer"}, "page": {"type": "integer"}}, "required": [], "additionalProperties": False},
    },
    "wc_get_order": {
        "description": "Get one WooCommerce order with line items, totals and customer details.",
        "input": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"], "additionalProperties": False},
    },
    "wc_update_order_status": {
        "description": "Change a WooCommerce order's status, e.g. status='completed' (live).",
        "write": True,
        "input": {"type": "object", "properties": {"order_id": {"type": "integer"}, "status": {"type": "string"}, "note": {"type": "string"}, "reason": {"type": "string"}}, "required": ["order_id", "status"], "additionalProperties": False},
    },
    "wc_sales_summary": {
        "description": "WooCommerce sales summary for the last N days (orders, revenue, average order value, items sold).",
        "input": {"type": "object", "properties": {"days": {"type": "integer", "description": "1–365 (default 30)."}}, "required": [], "additionalProperties": False},
    },

    # --- Onboarding ---
    "test_connection": {
        "description": "Diagnose the WordPress connection: plugin reachable, token valid, and whether the host strips the Authorization header. Run this first if anything fails.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },

    # --- Content & SEO (advanced) ---
    "bulk_find_replace": {
        "description": "Find-and-replace text across all posts/pages (live). dry_run=true previews counts without changing anything. Auto-backs-up content before applying.",
        "write": True,
        "input": {"type": "object", "properties": {
            "find": {"type": "string"}, "replace": {"type": "string"},
            "post_types": {"type": "array", "items": {"type": "string"}, "description": "Default ['post','page']."},
            "include_title": {"type": "boolean", "description": "Also replace inside titles."},
            "dry_run": {"type": "boolean", "description": "Preview only."},
            "reason": {"type": "string"},
        }, "required": ["find"], "additionalProperties": False},
    },
    "schedule_post": {
        "description": "Schedule a post to publish at a future time (live). Pass post_id to schedule an existing post, or title to create a new scheduled one.",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"}, "title": {"type": "string"}, "content": {"type": "string"},
            "type": {"type": "string", "description": "post|page (when creating)."},
            "publish_at": {"type": "string", "description": "ISO datetime, e.g. 2026-07-01T09:00:00."},
            "reason": {"type": "string"},
        }, "required": ["publish_at"], "additionalProperties": False},
    },
    "content_calendar": {"description": "List all scheduled (future) posts/pages with their publish dates.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "list_images_missing_alt": {
        "description": "List media-library images with no alt text, so AI can generate it.",
        "input": {"type": "object", "properties": {"per_page": {"type": "integer", "description": "1–300 (default 100)."}}, "required": [], "additionalProperties": False},
    },
    "set_image_alt": {
        "description": "Set image alt text — one (media_id + alt) or many (items:[{media_id, alt}]) (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "media_id": {"type": "integer"}, "alt": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object"}, "description": "[{media_id, alt}] for bulk."},
            "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "list_redirects": {"description": "List all 301/302 redirects BRING-DATA manages.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "add_redirect": {
        "description": "Add a redirect (live). from = path or full URL, to = destination, type = 301|302|307.",
        "write": True,
        "input": {"type": "object", "properties": {
            "from": {"type": "string", "description": "Source path (e.g. /old-page) or full URL."},
            "to": {"type": "string", "description": "Destination URL."},
            "type": {"type": "integer", "description": "301 (default), 302 or 307."}, "reason": {"type": "string"},
        }, "required": ["from", "to"], "additionalProperties": False},
    },
    "delete_redirect": {
        "description": "Delete a redirect by id (live).",
        "write": True,
        "input": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
    },
    "get_robots_txt": {"description": "Get robots.txt (custom override if set) + the public robots URL.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "update_robots_txt": {
        "description": "Set a custom robots.txt (live). Empty content resets to the WordPress default.",
        "write": True,
        "input": {"type": "object", "properties": {"content": {"type": "string"}, "reason": {"type": "string"}}, "required": ["content"], "additionalProperties": False},
    },
    "get_sitemaps": {"description": "Report sitemap URLs (WordPress core and/or Yoast) and whether the site is indexable.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "get_schema": {
        "description": "Get the custom JSON-LD structured data attached to a post.",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },
    "set_schema": {
        "description": "Attach custom JSON-LD structured data to a post for rich snippets (live). Empty string removes it.",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "schema": {"type": "string", "description": "JSON-LD as an object or JSON string."},
            "reason": {"type": "string"},
        }, "required": ["post_id", "schema"], "additionalProperties": False},
    },
    "set_social_meta": {
        "description": "Set OpenGraph / Twitter card meta for a post (live). Uses Yoast fields if active, else BRING-DATA's own tags.",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "og_title": {"type": "string"}, "og_description": {"type": "string"}, "og_image_url": {"type": "string"},
            "twitter_title": {"type": "string"}, "twitter_description": {"type": "string"}, "twitter_image_url": {"type": "string"},
            "reason": {"type": "string"},
        }, "required": ["post_id"], "additionalProperties": False},
    },

    # --- Structure & builders ---
    "build_gutenberg_page": {
        "description": "Build a page/post from a simple block list (live). blocks:[{type,...}] — type heading|paragraph|image|list|button|quote|html. Pass post_id (+append) to extend an existing post, or title to create one.",
        "write": True,
        "input": {"type": "object", "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}},
            "post_id": {"type": "integer"}, "append": {"type": "boolean"},
            "title": {"type": "string"}, "type": {"type": "string", "description": "page|post (when creating)."},
            "status": {"type": "string", "description": "draft|publish|pending|private."}, "reason": {"type": "string"},
        }, "required": ["blocks"], "additionalProperties": False},
    },
    "get_elementor_data": {
        "description": "Read a post's raw Elementor layout JSON (requires Elementor active).",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },
    "set_elementor_data": {
        "description": "Write a post's Elementor layout JSON and switch it to builder mode (live; requires Elementor).",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "elementor_data": {"type": "string", "description": "Elementor data as a JSON string or array."},
            "reason": {"type": "string"},
        }, "required": ["post_id", "elementor_data"], "additionalProperties": False},
    },
    "build_elementor_page": {
        "description": ("Build an Elementor page from a simple block list (live; Elementor FREE — no raw JSON needed). "
                        "title creates a new page, or post_id rebuilds one. canvas=true uses the blank Elementor Canvas "
                        "template (no header/footer) — ideal for landing pages. Free widgets only."),
        "write": True,
        "input": {"type": "object", "properties": {
            "title": {"type": "string", "description": "New page title (omit if using post_id)."},
            "post_id": {"type": "integer", "description": "Rebuild this existing page instead of creating one."},
            "blocks": {"type": "array", "items": {"type": "object"},
                       "description": "[{type, ...}] — type: heading{text,level,align} | paragraph/text{text} | button{text,link,align} | image{url,align} | spacer{size} | divider | video{url} | html{html}. Any block may add background (hex) + padding (px). MULTI-COLUMN: {type:'columns', columns:[ [..blocks..], {blocks:[..], width:60} ]} makes side-by-side columns (even widths, or per-column width percent)."},
            "type": {"type": "string", "description": "post type for new pages (default page)."},
            "status": {"type": "string", "description": "draft|publish|pending|private (default draft)."},
            "canvas": {"type": "boolean", "description": "Use the blank Elementor Canvas template (landing pages)."},
            "reason": {"type": "string"},
        }, "required": ["blocks"], "additionalProperties": False},
    },
    "list_cf7_forms": {
        "description": "List all Contact Form 7 forms (id, title, shortcode). Requires Contact Form 7 active (free).",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "get_cf7_form": {
        "description": "Get one Contact Form 7 form's full definition (form markup, mail settings, messages).",
        "input": {"type": "object", "properties": {"form_id": {"type": "integer"}}, "required": ["form_id"], "additionalProperties": False},
    },
    "create_cf7_form": {
        "description": ("Create a Contact Form 7 form (live; free). title required. Optional `form` = CF7 markup using CF7 tags "
                        "(e.g. [text* your-name], [email* your-email], [textarea your-message], [submit \"Send\"]) — a default "
                        "name/email/subject/message form is used if omitted. Optional `mail` overrides the notification email. Returns the shortcode to embed."),
        "write": True,
        "input": {"type": "object", "properties": {
            "title": {"type": "string"},
            "form": {"type": "string", "description": "CF7 form markup (CF7 tags + HTML). Kept verbatim."},
            "mail": {"type": "object", "description": "{subject, sender, recipient, body, additional_headers, use_html, exclude_blank}."},
            "mail_2": {"type": "object", "description": "Optional autoresponder email (same shape as mail)."},
            "messages": {"type": "object", "description": "Override CF7 status messages."},
            "reason": {"type": "string"},
        }, "required": ["title"], "additionalProperties": False},
    },
    "update_cf7_form": {
        "description": "Update a Contact Form 7 form (live). Any of title / form (markup) / mail / mail_2 / messages.",
        "write": True,
        "input": {"type": "object", "properties": {
            "form_id": {"type": "integer"}, "title": {"type": "string"}, "form": {"type": "string"},
            "mail": {"type": "object"}, "mail_2": {"type": "object"}, "messages": {"type": "object"},
            "reason": {"type": "string"},
        }, "required": ["form_id"], "additionalProperties": False},
    },
    "delete_cf7_form": {
        "description": "Permanently delete a Contact Form 7 form (live).",
        "write": True,
        "input": {"type": "object", "properties": {"form_id": {"type": "integer"}, "reason": {"type": "string"}},
                  "required": ["form_id"], "additionalProperties": False},
    },
    "get_custom_fields": {
        "description": "Read a post's custom fields — ACF fields (if active) plus visible post meta.",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },
    "set_custom_field": {
        "description": "Set a custom field on a post (live). key + value; acf=true writes via ACF's update_field.",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"}, "key": {"type": "string"},
            "value": {"description": "String, number, boolean or array."},
            "acf": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": ["post_id", "key"], "additionalProperties": False},
    },
    "list_acf_field_groups": {
        "description": "List ACF field groups and their fields (key, name, label, type) + where each applies. Requires ACF active.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "create_acf_field_group": {
        "description": ("Create an ACF field group — the field DEFINITIONS, not just values (live; requires ACF, works on the FREE "
                        "version). Imported into the DB so it's editable in WP-admin → ACF and its fields can then be read/written "
                        "with get_custom_fields / set_custom_field(acf=true). ACF free lacks Repeater/Flexible Content/Clone/Gallery "
                        "(those need ACF Pro)."),
        "write": True,
        "input": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Field group name, e.g. 'Landing Page Hero'."},
            "fields": {"type": "array", "items": {"type": "object"},
                       "description": "[{label, name?, type?, required?, instructions?, default_value?, choices?}]. type defaults to 'text' (text|textarea|number|email|url|image|file|select|checkbox|radio|true_false|wysiwyg|date_picker|color_picker|link|post_object|relationship etc.). name auto-derived from label if omitted."},
            "post_types": {"type": "array", "items": {"type": "string"}, "description": "Where the group shows, e.g. ['page','post','product']. Defaults to ['post']."},
            "location": {"type": "array", "items": {"type": "array", "items": {"type": "object"}},
                         "description": "Advanced: raw ACF location rule groups (OR of ANDs). Overrides post_types when set."},
            "key": {"type": "string", "description": "Optional explicit group key (auto-generated if omitted)."},
            "active": {"type": "boolean", "description": "Whether the group is active (default true)."},
            "reason": {"type": "string"},
        }, "required": ["title", "fields"], "additionalProperties": False},
    },
    "list_widget_areas": {"description": "List the theme's widget areas (sidebars) and the widgets in each.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "add_html_widget": {
        "description": "Add a Custom HTML widget to a widget area (live). sidebar_id + content (+ optional title).",
        "write": True,
        "input": {"type": "object", "properties": {
            "sidebar_id": {"type": "string", "description": "Widget area id (see list_widget_areas)."},
            "content": {"type": "string", "description": "HTML for the widget."},
            "title": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["sidebar_id", "content"], "additionalProperties": False},
    },
    "list_post_types": {"description": "List public post types incl. custom post types (slug, label, count). Use the slug as `type` in list_posts/create_post.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "list_page_templates": {"description": "List page templates on the active theme (+ Elementor Canvas/Full-Width if active). Use a returned slug as `template` in create_post/update_post for a full-width landing page.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},

    # --- Maintenance & safety ---
    "create_content_backup": {
        "description": "Snapshot all post/page content to a download-protected JSON backup (live).",
        "write": True,
        "input": {"type": "object", "properties": {"note": {"type": "string"}, "reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "list_backups": {"description": "List content backups BRING-DATA has created (newest first).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "db_status": {"description": "Report cleanable DB clutter: revisions, auto-drafts, trash, spam, transients.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "db_cleanup": {
        "description": "Delete database clutter (live). all=true cleans everything, or set individual flags. Back up first.",
        "write": True,
        "input": {"type": "object", "properties": {
            "all": {"type": "boolean"}, "revisions": {"type": "boolean"}, "auto_drafts": {"type": "boolean"},
            "spam": {"type": "boolean"}, "trash": {"type": "boolean"}, "transients": {"type": "boolean"},
            "orphan_meta": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "clear_cache": {
        "description": "Purge caches — WP Rocket, LiteSpeed, W3TC, WP Super Cache, SiteGround, Cache Enabler + object cache (live).",
        "write": True,
        "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "performance_audit": {"description": "Server-side performance signals: active plugins, autoloaded option size, object cache, PHP version + flagged issues.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "scan_broken_links": {
        "description": "Scan post/page content for broken links (4xx / unreachable). Optionally limit to one post_id.",
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer", "description": "Scan just this post."},
            "scan_posts": {"type": "integer", "description": "How many recent posts to scan (default 20)."},
            "limit": {"type": "integer", "description": "Max links to check, 1–100 (default 50)."},
        }, "required": [], "additionalProperties": False},
    },
    "list_forms": {"description": "List contact forms detected (Contact Form 7, WPForms, Gravity Forms).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "get_form_submissions": {
        "description": "Read form submissions/entries (Gravity, WPForms, or CF7-via-Flamingo). Pass form_id where supported.",
        "input": {"type": "object", "properties": {
            "form_id": {"type": "integer"}, "limit": {"type": "integer", "description": "1–100 (default 30)."},
        }, "required": [], "additionalProperties": False},
    },

    # --- Themes (advanced / FSE) ---
    "list_block_templates": {
        "description": "List FSE block templates or template parts (block themes, WP 5.9+).",
        "input": {"type": "object", "properties": {"type": {"type": "string", "description": "wp_template (default) | wp_template_part."}}, "required": [], "additionalProperties": False},
    },
    "get_block_template": {
        "description": "Get one FSE block template's content by id (e.g. 'theme//index').",
        "input": {"type": "object", "properties": {"id": {"type": "string"}, "type": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
    },
    "edit_block_template": {
        "description": "Create or overwrite an FSE block template/part for the active theme (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "e.g. index, single, header, footer."},
            "content": {"type": "string", "description": "Block markup."},
            "type": {"type": "string", "description": "wp_template (default) | wp_template_part."}, "reason": {"type": "string"},
        }, "required": ["slug", "content"], "additionalProperties": False},
    },
    "get_global_styles": {"description": "Read the theme.json user global styles (colors, typography, spacing) of a block theme.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "set_global_styles": {
        "description": "Update theme.json global styles (live; block themes). merge=true deep-merges with current.",
        "write": True,
        "input": {"type": "object", "properties": {
            "global_styles": {"description": "theme.json styles as an object or JSON string."},
            "merge": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": ["global_styles"], "additionalProperties": False},
    },

    # --- WooCommerce (bulk & more) ---
    "wc_bulk_update_products": {
        "description": "Bulk-update many products (live). Target by ids[] or filter; set fields and/or apply a price_adjust (percent/fixed).",
        "write": True,
        "input": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "filter": {"type": "object", "description": "{category, status, stock_status}."},
            "set": {"type": "object", "description": "{regular_price, sale_price, stock_quantity, stock_status, status, category}."},
            "price_adjust": {"type": "object", "description": "{type:percent|fixed, value, field:regular|sale}."},
            "limit": {"type": "integer"}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "wc_bulk_create_products": {
        "description": "Create many products in one call (live). products:[{name, regular_price, ...}].",
        "write": True,
        "input": {"type": "object", "properties": {"products": {"type": "array", "items": {"type": "object"}}, "reason": {"type": "string"}}, "required": ["products"], "additionalProperties": False},
    },
    "wc_export_products": {"description": "Export all products to a download-protected CSV; returns the file URL.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_import_products": {
        "description": "Import/upsert products by SKU from a CSV URL or rows[] (live).",
        "write": True,
        "input": {"type": "object", "properties": {
            "csv_url": {"type": "string"}, "rows": {"type": "array", "items": {"type": "object"}}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "wc_bulk_order_status": {
        "description": "Set the status of many orders at once (live). order_ids[] + status.",
        "write": True,
        "input": {"type": "object", "properties": {
            "order_ids": {"type": "array", "items": {"type": "integer"}}, "status": {"type": "string"}, "note": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["order_ids", "status"], "additionalProperties": False},
    },
    "wc_list_coupons": {"description": "List WooCommerce coupons (code, type, amount, expiry, usage).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_create_coupon": {
        "description": "Create a WooCommerce coupon (live). discount_type = percent|fixed_cart|fixed_product.",
        "write": True,
        "input": {"type": "object", "properties": {
            "code": {"type": "string"}, "discount_type": {"type": "string"}, "amount": {"type": "string"},
            "expires": {"type": "string", "description": "YYYY-MM-DD."}, "minimum_amount": {"type": "string"},
            "usage_limit": {"type": "integer"}, "free_shipping": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": ["code"], "additionalProperties": False},
    },
    "wc_delete_coupon": {
        "description": "Delete a WooCommerce coupon by id (live).",
        "write": True,
        "input": {"type": "object", "properties": {"coupon_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["coupon_id"], "additionalProperties": False},
    },
    "wc_list_product_categories": {"description": "List WooCommerce product categories.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_create_product_category": {
        "description": "Create a WooCommerce product category (live).",
        "write": True,
        "input": {"type": "object", "properties": {"name": {"type": "string"}, "parent": {"type": "integer"}, "description": {"type": "string"}, "reason": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
    },
    "wc_low_stock": {
        "description": "List products at or below a stock threshold (default 5).",
        "input": {"type": "object", "properties": {"threshold": {"type": "integer"}}, "required": [], "additionalProperties": False},
    },
    "wc_top_sellers": {
        "description": "List best-selling products by total sales (default top 10).",
        "input": {"type": "object", "properties": {"limit": {"type": "integer", "description": "1–50 (default 10)."}}, "required": [], "additionalProperties": False},
    },

    # --- Images ---
    "image_capabilities": {"description": "Report server image support (GD, Imagick, WebP) — check before running image operations.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "convert_to_webp": {
        "description": "Convert media images to WebP (live). One media_id or a batch (limit). replace=true swaps the attachment to WebP. Needs server WebP support.",
        "write": True,
        "input": {"type": "object", "properties": {
            "media_id": {"type": "integer"}, "limit": {"type": "integer", "description": "Batch size when no media_id (default 50)."},
            "quality": {"type": "integer", "description": "1–100 (default 82)."}, "replace": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "optimize_images": {
        "description": "Recompress media images at a target quality to shrink size (live). One media_id or a batch (limit).",
        "write": True,
        "input": {"type": "object", "properties": {
            "media_id": {"type": "integer"}, "limit": {"type": "integer"}, "quality": {"type": "integer", "description": "1–100 (default 75)."}, "reason": {"type": "string"},
        }, "required": [], "additionalProperties": False},
    },
    "resize_image": {
        "description": "Resize an image's original file to max_width/max_height (live). crop=true to hard-crop.",
        "write": True,
        "input": {"type": "object", "properties": {
            "media_id": {"type": "integer"}, "max_width": {"type": "integer"}, "max_height": {"type": "integer"}, "crop": {"type": "boolean"}, "reason": {"type": "string"},
        }, "required": ["media_id"], "additionalProperties": False},
    },
    "regenerate_thumbnails": {
        "description": "Regenerate all thumbnail sizes for one image (media_id) or a batch — run after a theme change (live).",
        "write": True,
        "input": {"type": "object", "properties": {"media_id": {"type": "integer"}, "limit": {"type": "integer"}, "reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "enable_lazy_load": {
        "description": "Toggle image lazy-loading site-wide (live). enabled defaults to true.",
        "write": True,
        "input": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },

    # --- SEO power ---
    "internal_link_audit": {"description": "Sitewide internal-link audit: orphan pages (no inbound internal links) + most-linked pages.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "content_audit": {
        "description": "Bulk content audit: thin content, missing meta description / focus keyword / featured image, duplicate titles.",
        "input": {"type": "object", "properties": {"type": {"type": "string", "description": "Post type (default post)."}, "limit": {"type": "integer", "description": "Max posts, up to 1000 (default 500)."}}, "required": [], "additionalProperties": False},
    },
    "get_404_log": {"description": "List logged 404 hits (URL, count, referrer) — feed these into the redirect manager.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "clear_404_log": {
        "description": "Clear the 404 log (live).",
        "write": True,
        "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "apply_schema_template": {
        "description": "Generate & attach JSON-LD from a template (live). template = Article|Product|FAQPage|LocalBusiness|BreadcrumbList; pass data for dynamic parts (e.g. FAQ questions, price).",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "template": {"type": "string", "description": "Article|Product|FAQPage|LocalBusiness|BreadcrumbList."},
            "data": {"type": "object", "description": "Template inputs, e.g. {questions:[{question,answer}]} or {price, currency}."},
            "reason": {"type": "string"},
        }, "required": ["post_id", "template"], "additionalProperties": False},
    },
    "get_hreflang": {
        "description": "Get the hreflang alternate-language links set on a post.",
        "input": {"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"], "additionalProperties": False},
    },
    "set_hreflang": {
        "description": "Set hreflang alternate-language links on a post (live). alternates:[{lang, url}]; empty array removes them.",
        "write": True,
        "input": {"type": "object", "properties": {
            "post_id": {"type": "integer"},
            "alternates": {"type": "array", "items": {"type": "object"}, "description": "[{lang:'en', url:'https://...'}]."},
            "reason": {"type": "string"},
        }, "required": ["post_id", "alternates"], "additionalProperties": False},
    },

    # --- v1.6 content/health/comms/woo/google ---
    "find_stale_content": {"description": "Find decaying content — posts not updated in N days (oldest first) + word counts.", "input": {"type": "object", "properties": {"days": {"type": "integer", "description": "Default 180."}, "type": {"type": "string"}, "limit": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    "restore_revision": {"description": "Restore a post to a prior revision (live).", "write": True, "input": {"type": "object", "properties": {"post_id": {"type": "integer"}, "revision_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["post_id", "revision_id"], "additionalProperties": False}},
    "list_reusable_blocks": {"description": "List reusable blocks (wp_block).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "create_reusable_block": {"description": "Create a reusable block (live).", "write": True, "input": {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "reason": {"type": "string"}}, "required": ["title", "content"], "additionalProperties": False}},
    "list_taxonomies": {"description": "List public taxonomies (incl. custom) with their post types.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "create_term": {"description": "Create a term in any taxonomy (live).", "write": True, "input": {"type": "object", "properties": {"taxonomy": {"type": "string"}, "name": {"type": "string"}, "parent": {"type": "integer"}, "description": {"type": "string"}, "reason": {"type": "string"}}, "required": ["taxonomy", "name"], "additionalProperties": False}},
    "assign_terms": {"description": "Assign taxonomy terms to a post (live). append=true to add.", "write": True, "input": {"type": "object", "properties": {"post_id": {"type": "integer"}, "taxonomy": {"type": "string"}, "terms": {"type": "array", "items": {"type": "string"}}, "append": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["post_id", "taxonomy", "terms"], "additionalProperties": False}},
    "site_health": {"description": "Site Health: WP/PHP/MySQL versions, HTTPS, debug, object cache, updates, cron.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "accessibility_audit": {"description": "Heuristic a11y scan: images missing alt, vague link text, site language.", "input": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    "configure_smtp": {"description": "Configure SMTP email delivery via a must-use plugin (live).", "write": True, "input": {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}, "username": {"type": "string"}, "password": {"type": "string"}, "encryption": {"type": "string", "description": "tls|ssl|'' (default tls)."}, "from_email": {"type": "string"}, "from_name": {"type": "string"}, "reason": {"type": "string"}}, "required": ["host", "username", "password"], "additionalProperties": False}},
    "send_test_email": {"description": "Send a test email to confirm delivery (live).", "write": True, "input": {"type": "object", "properties": {"to": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "purge_spam": {"description": "Permanently delete all spam comments (live).", "write": True, "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "export_wxr": {"description": "Export all content to a WordPress WXR (.xml); returns a download-protected URL.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_list_reviews": {"description": "List WooCommerce product reviews by status.", "input": {"type": "object", "properties": {"status": {"type": "string"}, "per_page": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    "wc_moderate_review": {"description": "Moderate a WooCommerce review (live). status=approve|hold|spam|trash.", "write": True, "input": {"type": "object", "properties": {"review_id": {"type": "integer"}, "status": {"type": "string"}}, "required": ["review_id", "status"], "additionalProperties": False}},
    "wc_refund_order": {"description": "Refund a WooCommerce order (live). amount defaults to full remaining.", "write": True, "input": {"type": "object", "properties": {"order_id": {"type": "integer"}, "amount": {"type": "number"}, "reason": {"type": "string"}, "restock": {"type": "boolean"}, "refund_payment": {"type": "boolean"}}, "required": ["order_id"], "additionalProperties": False}},
    "set_google_service_account": {"description": "Store the Google service-account JSON for the Indexing API (live).", "write": True, "input": {"type": "object", "properties": {"service_account_json": {"description": "Full service-account JSON (object or string)."}}, "required": ["service_account_json"], "additionalProperties": False}},
    "index_url": {"description": "Submit a URL to Google's Indexing API (live). type=URL_UPDATED|URL_DELETED.", "write": True, "input": {"type": "object", "properties": {"url": {"type": "string"}, "type": {"type": "string"}, "reason": {"type": "string"}}, "required": ["url"], "additionalProperties": False}},
    "index_status": {"description": "Check a URL's last Google indexing notification.", "input": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}},
    "pagespeed": {"description": "Real Core Web Vitals via Google PageSpeed Insights (Lighthouse) for any public URL. strategy=mobile|desktop.", "input": {"type": "object", "properties": {"url": {"type": "string"}, "strategy": {"type": "string"}, "api_key": {"type": "string", "description": "Optional, raises quota."}}, "required": ["url"], "additionalProperties": False}},

    # --- Power tools ---
    "rest_request": {"description": "Call ANY WordPress REST route (live escape hatch, runs as admin) — reaches any plugin's API. method/path/params/body.", "write": True,
        "input": {"type": "object", "properties": {"method": {"type": "string", "description": "GET|POST|PUT|PATCH|DELETE."}, "path": {"type": "string", "description": "e.g. /wp/v2/posts, /wc/v3/products."}, "params": {"type": "object"}, "body": {"type": "object"}}, "required": ["path"], "additionalProperties": False}},
    "db_query": {"description": "Run a READ-ONLY SQL query (single SELECT/SHOW/DESCRIBE/EXPLAIN/WITH) against the WP database. Great for custom reports.", "write": True,
        "input": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False}},
    "get_option": {"description": "Read any wp_options value by key.", "input": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False}},
    "set_option": {"description": "Set any wp_options value (live).", "write": True, "input": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"description": "Any JSON value."}, "reason": {"type": "string"}}, "required": ["key", "value"], "additionalProperties": False}},
    "duplicate_post": {"description": "Duplicate a post/page as a new draft (content + taxonomies + meta) (live).", "write": True, "input": {"type": "object", "properties": {"post_id": {"type": "integer"}, "title": {"type": "string"}, "reason": {"type": "string"}}, "required": ["post_id"], "additionalProperties": False}},
    "bulk_delete_posts": {"description": "Bulk trash/delete posts (live). ids[] (safest) or filter type/status with confirm=true; force=true = permanent.", "write": True,
        "input": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}, "type": {"type": "string"}, "status": {"type": "string"}, "max": {"type": "integer"}, "confirm": {"type": "boolean"}, "force": {"type": "boolean"}, "reason": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "list_cron": {"description": "List scheduled WP-Cron events (hook, next run, schedule).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "run_cron": {"description": "Run a scheduled cron hook now (live).", "write": True, "input": {"type": "object", "properties": {"hook": {"type": "string"}, "reason": {"type": "string"}}, "required": ["hook"], "additionalProperties": False}},
    "clear_cron": {"description": "Unschedule all events for a cron hook (live).", "write": True, "input": {"type": "object", "properties": {"hook": {"type": "string"}, "reason": {"type": "string"}}, "required": ["hook"], "additionalProperties": False}},
    "maintenance_mode": {"description": "Get or set front-end maintenance mode. Pass enabled to set (+message); omit to read status.", "write": True,
        "input": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "message": {"type": "string"}, "reason": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "reply_to_comment": {"description": "Post an approved reply to a comment (live).", "write": True, "input": {"type": "object", "properties": {"comment_id": {"type": "integer"}, "content": {"type": "string"}, "author": {"type": "string"}, "author_email": {"type": "string"}, "reason": {"type": "string"}}, "required": ["comment_id", "content"], "additionalProperties": False}},
    "bulk_comment_action": {"description": "Apply an action to many comments (live). action=approve|unapprove|spam|trash|delete.", "write": True, "input": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}, "action": {"type": "string"}, "reason": {"type": "string"}}, "required": ["ids", "action"], "additionalProperties": False}},
    "media_replace": {"description": "Replace a media file in place from a URL — keeps the same id/URL (live).", "write": True, "input": {"type": "object", "properties": {"media_id": {"type": "integer"}, "url": {"type": "string"}, "reason": {"type": "string"}}, "required": ["media_id", "url"], "additionalProperties": False}},
    "bulk_set_image_alt": {"description": "Apply alt text to many media items at once (live). items:[{id, alt}].", "write": True, "input": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}, "reason": {"type": "string"}}, "required": ["items"], "additionalProperties": False}},
    "list_block_patterns": {"description": "List registered Gutenberg block patterns.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "insert_block_pattern": {"description": "Insert a registered block pattern into a post (live). append=true adds to end.", "write": True, "input": {"type": "object", "properties": {"post_id": {"type": "integer"}, "pattern": {"type": "string"}, "append": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["post_id", "pattern"], "additionalProperties": False}},
    "list_roles": {"description": "List user roles and their capabilities.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "set_role_capabilities": {"description": "Add/remove capabilities on a role (live).", "write": True, "input": {"type": "object", "properties": {"role": {"type": "string"}, "add": {"type": "array", "items": {"type": "string"}}, "remove": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}, "required": ["role"], "additionalProperties": False}},
    "create_role": {"description": "Create a custom user role (live).", "write": True, "input": {"type": "object", "properties": {"slug": {"type": "string"}, "name": {"type": "string"}, "capabilities": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}, "required": ["slug", "name"], "additionalProperties": False}},
    "flush_rewrite_rules": {"description": "Flush rewrite rules to fix broken permalinks (live).", "write": True, "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "get_debug_log": {"description": "Read the tail of wp-content/debug.log + whether WP_DEBUG is on.", "input": {"type": "object", "properties": {"lines": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    "wc_list_customers": {"description": "List WooCommerce customers with order count + total spent.", "input": {"type": "object", "properties": {"per_page": {"type": "integer"}, "page": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    "wc_list_variations": {"description": "List a variable product's variations (sku, price, stock, attributes).", "input": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"], "additionalProperties": False}},
    "wc_update_variation": {"description": "Update a product variation (live).", "write": True, "input": {"type": "object", "properties": {"variation_id": {"type": "integer"}, "regular_price": {"type": "string"}, "sale_price": {"type": "string"}, "sku": {"type": "string"}, "stock_quantity": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["variation_id"], "additionalProperties": False}},
    "wc_list_shipping_zones": {"description": "List WooCommerce shipping zones, regions and methods.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_list_tax_rates": {"description": "List WooCommerce tax rates.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_list_webhooks": {"description": "List WooCommerce webhooks.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "wc_create_webhook": {"description": "Create a WooCommerce webhook (live). topic + delivery_url.", "write": True, "input": {"type": "object", "properties": {"topic": {"type": "string"}, "delivery_url": {"type": "string"}, "name": {"type": "string"}, "status": {"type": "string"}, "reason": {"type": "string"}}, "required": ["topic", "delivery_url"], "additionalProperties": False}},

    # --- Security suite (whole-site) ---
    "full_security_audit": {"description": "WHOLE-SITE security audit — runs every check (vulns, file integrity, permissions, hardening, logins, suspicious activity, secrets, mixed content) + HTTP headers, scored 0–100 with a grade. auto_fix=true applies safe hardening; blocklist_api_key adds a Google Safe Browsing check.", "write": True,
        "input": {"type": "object", "properties": {"auto_fix": {"type": "boolean"}, "blocklist_api_key": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "vulnerability_scan": {"description": "Scan core + plugins + themes for OUTDATED (vulnerable) versions — the top attack vector.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "file_integrity_scan": {"description": "Scan wp-content for malware signs: PHP in uploads/, recently-modified PHP, world-writable files.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "file_permissions_audit": {"description": "Audit file/dir permissions (wp-config, .htaccess, wp-content, uploads, root); flag world-writable/loose perms.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "hardening_audit": {"description": "Config hardening checks: file-edit disabled, debug off, XML-RPC, open registration, default prefix, unique salts, readme/version leak.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "security_headers_check": {"description": "Check the site's HTTP response for HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "login_security": {"description": "Audit admin accounts: guessable usernames, admin count, active sessions.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "force_logout_all": {"description": "Destroy ALL users' active sessions (live) — forces a global re-login (use after a breach).", "write": True, "input": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": [], "additionalProperties": False}},
    "suspicious_activity_scan": {"description": "Hunt for compromise: code in autoloaded options, suspicious cron hooks, recently-created admins.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "secrets_scan": {"description": "Check auth salts are set/unique/non-default and wp-config.php permissions are locked down.", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "mixed_content_scan": {"description": "Find published posts referencing insecure http:// URLs on your own https domain (mixed content).", "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    "blocklist_check": {"description": "Check if the domain is flagged by Google Safe Browsing. Pass api_key (free) to run the live check.", "input": {"type": "object", "properties": {"api_key": {"type": "string"}}, "required": [], "additionalProperties": False}},
}

HANDLERS = {
    "site_info": site_info,
    "list_posts": list_posts,
    "get_post": get_post,
    "recent_changes": recent_changes,
    "update_seo_meta": update_seo_meta,
    "insert_internal_link": insert_internal_link,
    # content
    "create_post": create_post,
    "update_post": update_post,
    "delete_post": delete_post,
    "get_post_revisions": get_post_revisions,
    # media
    "upload_media": upload_media,
    "list_media": list_media,
    "delete_media": delete_media,
    # taxonomies
    "list_categories": list_categories,
    "create_category": create_category,
    "list_tags": list_tags,
    "create_tag": create_tag,
    # menus
    "list_menus": list_menus,
    "get_menu": get_menu,
    "update_menu": update_menu,
    # plugins & themes
    "list_plugins": list_plugins,
    "activate_plugin": activate_plugin,
    "deactivate_plugin": deactivate_plugin,
    "list_themes": list_themes,
    "activate_theme": activate_theme,
    # users
    "list_users": list_users,
    "create_user": create_user,
    "update_user_role": update_user_role,
    # settings
    "get_site_settings": get_site_settings,
    "update_site_settings": update_site_settings,
    # comments
    "list_comments": list_comments,
    "approve_comment": approve_comment,
    "delete_comment": delete_comment,
    # seo extended
    "bulk_update_seo_meta": bulk_update_seo_meta,
    "get_seo_score": get_seo_score,
    # pages
    "list_pages": list_pages,
    "set_homepage": set_homepage,
    # menus (build)
    "create_menu": create_menu,
    "delete_menu": delete_menu,
    "list_menu_locations": list_menu_locations,
    "assign_menu_location": assign_menu_location,
    # plugins (install/update/delete)
    "install_plugin": install_plugin,
    "update_plugin": update_plugin,
    "update_all_plugins": update_all_plugins,
    "delete_plugin": delete_plugin,
    "check_updates": check_updates,
    # themes (install/update/delete/customize/files/child)
    "install_theme": install_theme,
    "update_theme": update_theme,
    "delete_theme": delete_theme,
    "customize_theme": customize_theme,
    "get_theme_file": get_theme_file,
    # security
    "security_scan": security_scan,
    "core_integrity_check": core_integrity_check,
    "audit_users": audit_users,
    "harden_site": harden_site,
    "malware_scan": malware_scan,
    "ssl_check": ssl_check,
    # woocommerce
    "wc_list_products": wc_list_products,
    "wc_get_product": wc_get_product,
    "wc_create_product": wc_create_product,
    "wc_update_product": wc_update_product,
    "wc_list_orders": wc_list_orders,
    "wc_get_order": wc_get_order,
    "wc_update_order_status": wc_update_order_status,
    "wc_sales_summary": wc_sales_summary,
    # onboarding
    "test_connection": test_connection,
    # content & seo (advanced)
    "bulk_find_replace": bulk_find_replace,
    "schedule_post": schedule_post,
    "content_calendar": content_calendar,
    "list_images_missing_alt": list_images_missing_alt,
    "set_image_alt": set_image_alt,
    "list_redirects": list_redirects,
    "add_redirect": add_redirect,
    "delete_redirect": delete_redirect,
    "get_robots_txt": get_robots_txt,
    "update_robots_txt": update_robots_txt,
    "get_sitemaps": get_sitemaps,
    "get_schema": get_schema,
    "set_schema": set_schema,
    "set_social_meta": set_social_meta,
    # structure & builders
    "build_gutenberg_page": build_gutenberg_page,
    "get_elementor_data": get_elementor_data,
    "set_elementor_data": set_elementor_data,
    "build_elementor_page": build_elementor_page,
    "list_cf7_forms": list_cf7_forms,
    "get_cf7_form": get_cf7_form,
    "create_cf7_form": create_cf7_form,
    "update_cf7_form": update_cf7_form,
    "delete_cf7_form": delete_cf7_form,
    "get_custom_fields": get_custom_fields,
    "set_custom_field": set_custom_field,
    "list_acf_field_groups": list_acf_field_groups,
    "create_acf_field_group": create_acf_field_group,
    "list_widget_areas": list_widget_areas,
    "add_html_widget": add_html_widget,
    "list_post_types": list_post_types,
    "list_page_templates": list_page_templates,
    # maintenance & safety
    "create_content_backup": create_content_backup,
    "list_backups": list_backups,
    "db_status": db_status,
    "db_cleanup": db_cleanup,
    "clear_cache": clear_cache,
    "performance_audit": performance_audit,
    "scan_broken_links": scan_broken_links,
    "list_forms": list_forms,
    "get_form_submissions": get_form_submissions,
    # themes (advanced / FSE)
    "list_block_templates": list_block_templates,
    "get_block_template": get_block_template,
    "edit_block_template": edit_block_template,
    "get_global_styles": get_global_styles,
    "set_global_styles": set_global_styles,
    # woocommerce (bulk & more)
    "wc_bulk_update_products": wc_bulk_update_products,
    "wc_bulk_create_products": wc_bulk_create_products,
    "wc_export_products": wc_export_products,
    "wc_import_products": wc_import_products,
    "wc_bulk_order_status": wc_bulk_order_status,
    "wc_list_coupons": wc_list_coupons,
    "wc_create_coupon": wc_create_coupon,
    "wc_delete_coupon": wc_delete_coupon,
    "wc_list_product_categories": wc_list_product_categories,
    "wc_create_product_category": wc_create_product_category,
    "wc_low_stock": wc_low_stock,
    "wc_top_sellers": wc_top_sellers,
    # images
    "image_capabilities": image_capabilities,
    "convert_to_webp": convert_to_webp,
    "optimize_images": optimize_images,
    "resize_image": resize_image,
    "regenerate_thumbnails": regenerate_thumbnails,
    "enable_lazy_load": enable_lazy_load,
    # seo power
    "internal_link_audit": internal_link_audit,
    "content_audit": content_audit,
    "get_404_log": get_404_log,
    "clear_404_log": clear_404_log,
    "apply_schema_template": apply_schema_template,
    "get_hreflang": get_hreflang,
    "set_hreflang": set_hreflang,
    # v1.6
    "find_stale_content": find_stale_content,
    "restore_revision": restore_revision,
    "list_reusable_blocks": list_reusable_blocks,
    "create_reusable_block": create_reusable_block,
    "list_taxonomies": list_taxonomies,
    "create_term": create_term,
    "assign_terms": assign_terms,
    "site_health": site_health,
    "accessibility_audit": accessibility_audit,
    "configure_smtp": configure_smtp,
    "send_test_email": send_test_email,
    "purge_spam": purge_spam,
    "export_wxr": export_wxr,
    "wc_list_reviews": wc_list_reviews,
    "wc_moderate_review": wc_moderate_review,
    "wc_refund_order": wc_refund_order,
    "set_google_service_account": set_google_service_account,
    "index_url": index_url,
    "index_status": index_status,
    "pagespeed": pagespeed,
    # power tools
    "rest_request": rest_request, "db_query": db_query,
    "get_option": get_option, "set_option": set_option,
    "duplicate_post": duplicate_post, "bulk_delete_posts": bulk_delete_posts,
    "list_cron": list_cron, "run_cron": run_cron, "clear_cron": clear_cron,
    "maintenance_mode": maintenance_mode,
    "reply_to_comment": reply_to_comment, "bulk_comment_action": bulk_comment_action,
    "media_replace": media_replace, "bulk_set_image_alt": bulk_set_image_alt,
    "list_block_patterns": list_block_patterns, "insert_block_pattern": insert_block_pattern,
    "list_roles": list_roles, "set_role_capabilities": set_role_capabilities, "create_role": create_role,
    "flush_rewrite_rules": flush_rewrite_rules, "get_debug_log": get_debug_log,
    # WooCommerce depth
    "wc_list_customers": wc_list_customers, "wc_list_variations": wc_list_variations,
    "wc_update_variation": wc_update_variation, "wc_list_shipping_zones": wc_list_shipping_zones,
    "wc_list_tax_rates": wc_list_tax_rates, "wc_list_webhooks": wc_list_webhooks,
    "wc_create_webhook": wc_create_webhook,
    # security suite
    "full_security_audit": full_security_audit, "vulnerability_scan": vulnerability_scan,
    "file_integrity_scan": file_integrity_scan, "file_permissions_audit": file_permissions_audit,
    "hardening_audit": hardening_audit, "security_headers_check": security_headers_check,
    "login_security": login_security, "force_logout_all": force_logout_all,
    "suspicious_activity_scan": suspicious_activity_scan, "secrets_scan": secrets_scan,
    "mixed_content_scan": mixed_content_scan, "blocklist_check": blocklist_check,
}

registry.register(
    Connector(
        slug="wordpress",
        label="WordPress",
        auth="api_key",
        cred_fields=["site_url", "api_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
        description=(
            'Reads and edits a WordPress site through the TechShu SEO Bridge plugin — posts, '
            'pages, media, menus, Yoast SEO meta, WooCommerce catalogue and security audits.'
        ),
        category='Content',
    )
)
