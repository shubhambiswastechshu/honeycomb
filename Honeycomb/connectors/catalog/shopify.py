"""Shopify connector (Admin REST API, api_key).

Reads the store: products and variants, orders and their line items, customers,
collections, inventory, discounts, abandoned checkouts, and rolled-up sales.

Auth is a Shopify Admin API access token from a custom app, sent as
``X-Shopify-Access-Token``, plus the store's ``shop_domain``. Nothing here
writes: every tool is a GET, no catalog entry carries ``write``, and the
registry therefore derives an empty ``write_tools``. A token minted for this
connector only needs the ``read_*`` scopes.

Two things are worth knowing about this file.

**The shop domain is caller-supplied and ends up in a URL**, which is an SSRF
hole if taken at face value -- a "domain" of ``169.254.169.254`` would point
this server's own HTTP client at the cloud metadata endpoint with whatever
token it holds. ``_shop_host`` therefore accepts nothing but a
``*.myshopify.com`` hostname, which is the only host a Shopify Admin token is
valid against anyway.

**Shopify paginates by opaque cursor, not by page number.** The ``Link`` header
carries ``page_info`` and Shopify rejects it alongside most other filters, so
``_paged`` sends the filters on the first call and only ``limit`` plus
``page_info`` thereafter. Callers get a ``next_page_info`` back and can pass it
in to continue.
"""
import re

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

SLUG = "shopify"
# Pinned rather than "latest": Shopify retires a version a year after release
# and an unpinned call starts failing on their schedule, not ours.
API_VERSION = "2024-10"

# A Shopify store handle: letters, digits and hyphens. The suffix is fixed, so
# the only thing the caller controls is the subdomain label.
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$", re.IGNORECASE)
_LINK_NEXT = re.compile(r'<[^>]*[?&]page_info=([^&>]+)[^>]*>;\s*rel="next"')


# --------------------------------------------------------------------------- #
# Auth and transport
# --------------------------------------------------------------------------- #
def _shop_host(conn: Connection) -> str:
    """The store's hostname, validated down to a myshopify.com subdomain.

    Anything else is refused. This value is concatenated into the request URL,
    so accepting a free-form host would let a saved credential aim this
    server's HTTP client at an arbitrary address -- link-local metadata
    included -- with an auth header attached.
    """
    raw = str(conn.creds().get("shop_domain") or "").strip().lower()
    if not raw:
        raise ConnectorError("Not connected: missing shop_domain.")
    # Accept what people paste: a bare handle, the full host, or a URL.
    raw = raw.replace("https://", "").replace("http://", "").strip("/")
    raw = raw.split("/")[0]
    if raw.endswith(".myshopify.com"):
        handle = raw[: -len(".myshopify.com")]
    else:
        handle = raw
    if not _SHOP_RE.match(handle):
        raise ConnectorError(
            "shop_domain must be your myshopify.com store, e.g. "
            "'acme.myshopify.com' or just 'acme'."
        )
    return handle + ".myshopify.com"


def _headers(conn: Connection) -> dict:
    token = str(conn.creds().get("access_token") or "").strip()
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return {"X-Shopify-Access-Token": token, "Accept": "application/json"}


def _fail(res) -> None:
    """Shopify's own message, truncated: this reaches the AI client and the log."""
    try:
        body = res.json()
        message = body.get("errors") or body.get("error") or res.text[:300]
    except ValueError:
        message = res.text[:300]
    if res.status_code == 401:
        raise ConnectorError("Shopify rejected the access token (401).")
    if res.status_code == 403:
        raise ConnectorError(
            "Shopify refused this call (403) -- the app's token is probably "
            f"missing a read scope. {str(message)[:200]}"
        )
    if res.status_code == 429:
        raise ConnectorError("Shopify rate limit hit (429). Try again shortly.")
    raise ConnectorError(f"Shopify {res.status_code}: {str(message)[:300]}")


async def _get(conn: Connection, db, path: str, params: dict | None = None) -> tuple[dict, str]:
    """One GET. Returns the body and the next page_info, if Shopify offered one."""
    url = f"https://{_shop_host(conn)}/admin/api/{API_VERSION}/{path.lstrip('/')}"
    async with limit_for(url):
        try:
            res = await http_get(url, headers=_headers(conn), params=params or {})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    match = _LINK_NEXT.search(res.headers.get("Link", "") or "")
    return res.json(), (match.group(1) if match else "")


async def _paged(
    conn: Connection, db, path: str, key: str, params: dict, limit: int
) -> tuple[list, str]:
    """A single page. Cursor and filters are mutually exclusive in Shopify's API.

    When the caller passes a page_info the filters are dropped, because Shopify
    rejects the combination outright rather than ignoring the extras.
    """
    cursor = str(params.pop("page_info", "") or "").strip()
    query = {"limit": limit, "page_info": cursor} if cursor else dict(params, limit=limit)
    body, nxt = await _get(conn, db, path, query)
    return body.get(key, []) or [], nxt


# --------------------------------------------------------------------------- #
# Argument helpers
# --------------------------------------------------------------------------- #
def _limit(args: dict, default: int = 50, max_value: int = 250) -> int:
    try:
        return max(1, min(int((args or {}).get("limit", default)), max_value))
    except (TypeError, ValueError):
        return default


def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _opt(args: dict, *keys: str) -> dict:
    """Pass through only the filters the caller actually supplied."""
    out = {}
    for k in keys:
        v = (args or {}).get(k)
        if v is not None and str(v).strip() != "":
            out[k] = v
    return out


def _money(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Shaping -- Shopify rows are enormous; these keep what a caller reads
# --------------------------------------------------------------------------- #
def _variant(v: dict) -> dict:
    return {
        "id": v.get("id"),
        "title": v.get("title"),
        "sku": v.get("sku"),
        "price": _money(v.get("price")),
        "compare_at_price": _money(v.get("compare_at_price")),
        "inventory_quantity": v.get("inventory_quantity"),
        "inventory_item_id": v.get("inventory_item_id"),
        "barcode": v.get("barcode"),
        "requires_shipping": v.get("requires_shipping"),
    }


def _product(p: dict, with_variants: bool = True) -> dict:
    out = {
        "id": p.get("id"),
        "title": p.get("title"),
        "handle": p.get("handle"),
        "status": p.get("status"),
        "vendor": p.get("vendor"),
        "product_type": p.get("product_type"),
        "tags": p.get("tags"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "published_at": p.get("published_at"),
        "image": (p.get("image") or {}).get("src"),
        "variant_count": len(p.get("variants", []) or []),
    }
    if with_variants:
        out["variants"] = [_variant(v) for v in (p.get("variants") or [])[:100]]
    return out


def _order(o: dict, with_lines: bool = True) -> dict:
    out = {
        "id": o.get("id"),
        "name": o.get("name"),
        "created_at": o.get("created_at"),
        "processed_at": o.get("processed_at"),
        "cancelled_at": o.get("cancelled_at"),
        "financial_status": o.get("financial_status"),
        "fulfillment_status": o.get("fulfillment_status"),
        "currency": o.get("currency"),
        "total_price": _money(o.get("total_price")),
        "subtotal_price": _money(o.get("subtotal_price")),
        "total_discounts": _money(o.get("total_discounts")),
        "total_tax": _money(o.get("total_tax")),
        "line_item_count": len(o.get("line_items", []) or []),
        # The customer object is mostly PII that nobody asked for; the id and a
        # display name are what a caller needs to join on.
        "customer_id": (o.get("customer") or {}).get("id"),
        "customer_email": o.get("email"),
        "source_name": o.get("source_name"),
        "tags": o.get("tags"),
    }
    if with_lines:
        out["line_items"] = [
            {
                "id": li.get("id"),
                "title": li.get("title"),
                "sku": li.get("sku"),
                "quantity": li.get("quantity"),
                "price": _money(li.get("price")),
                "product_id": li.get("product_id"),
                "variant_id": li.get("variant_id"),
            }
            for li in (o.get("line_items") or [])[:250]
        ]
    return out


def _customer(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "email": c.get("email"),
        "first_name": c.get("first_name"),
        "last_name": c.get("last_name"),
        "orders_count": c.get("orders_count"),
        "total_spent": _money(c.get("total_spent")),
        "state": c.get("state"),
        "verified_email": c.get("verified_email"),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "tags": c.get("tags"),
    }


# =========================================================================== #
# Store
# =========================================================================== #
async def shop_info(conn: Connection, db, args: dict) -> dict:
    """The store itself: name, plan, currency, timezone, domains."""

    async def _load():
        body, _ = await _get(conn, db, "shop.json")
        s = body.get("shop", {}) or {}
        return {
            "id": s.get("id"),
            "name": s.get("name"),
            "domain": s.get("domain"),
            "myshopify_domain": s.get("myshopify_domain"),
            "plan_name": s.get("plan_name"),
            "currency": s.get("currency"),
            "money_format": s.get("money_format"),
            "timezone": s.get("iana_timezone"),
            "country": s.get("country_name"),
            "created_at": s.get("created_at"),
        }

    return await cached(SLUG, conn.id, "shop_info", TTL_MEDIUM, _load)


# =========================================================================== #
# Products
# =========================================================================== #
async def list_products(conn: Connection, db, args: dict) -> dict:
    limit = _limit(args, 50, 250)
    filters = _opt(args, "status", "vendor", "product_type", "collection_id",
                   "created_at_min", "updated_at_min", "page_info")

    async def _load():
        rows, nxt = await _paged(conn, db, "products.json", "products", dict(filters), limit)
        return {
            "row_count": len(rows),
            "next_page_info": nxt,
            "products": [_product(p) for p in rows],
        }

    return await cached(SLUG, conn.id, "list_products", TTL_SHORT, _load,
                        args={"lim": limit, "f": filters})


async def get_product(conn: Connection, db, args: dict) -> dict:
    pid = _require(args, "product_id", "8123456789")

    async def _load():
        body, _ = await _get(conn, db, f"products/{pid}.json")
        p = body.get("product", {}) or {}
        out = _product(p)
        out["body_html"] = (p.get("body_html") or "")[:4000]
        out["images"] = [i.get("src") for i in (p.get("images") or [])[:20]]
        out["options"] = [
            {"name": o.get("name"), "values": o.get("values")}
            for o in (p.get("options") or [])
        ]
        return out

    return await cached(SLUG, conn.id, "get_product", TTL_SHORT, _load, args={"p": pid})


async def search_products(conn: Connection, db, args: dict) -> dict:
    """Title match. Shopify's REST product list has no free-text search, so the
    page is filtered here -- which means it searches the page, not the catalog."""
    needle = _require(args, "query", "hoodie").lower()
    limit = _limit(args, 50, 250)

    async def _load():
        rows, nxt = await _paged(conn, db, "products.json", "products", {}, limit)
        hits = [p for p in rows if needle in str(p.get("title", "")).lower()]
        return {
            "query": needle,
            "scanned": len(rows),
            "row_count": len(hits),
            "next_page_info": nxt,
            "note": "Matches titles within the scanned page. Raise limit or "
                    "page through with next_page_info to search further.",
            "products": [_product(p, with_variants=False) for p in hits],
        }

    return await cached(SLUG, conn.id, "search_products", TTL_SHORT, _load,
                        args={"q": needle, "lim": limit})


async def count_products(conn: Connection, db, args: dict) -> dict:
    filters = _opt(args, "status", "vendor", "product_type", "collection_id")

    async def _load():
        body, _ = await _get(conn, db, "products/count.json", filters)
        return {"count": body.get("count"), "filters": filters}

    return await cached(SLUG, conn.id, "count_products", TTL_SHORT, _load,
                        args={"f": filters})


# =========================================================================== #
# Orders
# =========================================================================== #
async def list_orders(conn: Connection, db, args: dict) -> dict:
    limit = _limit(args, 50, 250)
    filters = _opt(args, "status", "financial_status", "fulfillment_status",
                   "created_at_min", "created_at_max", "updated_at_min",
                   "since_id", "page_info")
    # Shopify defaults to open orders only, which silently hides the archive.
    filters.setdefault("status", "any")

    async def _load():
        rows, nxt = await _paged(conn, db, "orders.json", "orders", dict(filters), limit)
        return {
            "row_count": len(rows),
            "next_page_info": nxt,
            "orders": [_order(o, with_lines=False) for o in rows],
        }

    return await cached(SLUG, conn.id, "list_orders", TTL_SHORT, _load,
                        args={"lim": limit, "f": filters})


async def get_order(conn: Connection, db, args: dict) -> dict:
    oid = _require(args, "order_id", "5123456789")

    async def _load():
        body, _ = await _get(conn, db, f"orders/{oid}.json")
        o = body.get("order", {}) or {}
        out = _order(o)
        out["shipping_lines"] = [
            {"title": s.get("title"), "price": _money(s.get("price"))}
            for s in (o.get("shipping_lines") or [])
        ]
        out["discount_codes"] = o.get("discount_codes") or []
        out["refunds"] = len(o.get("refunds", []) or [])
        return out

    return await cached(SLUG, conn.id, "get_order", TTL_SHORT, _load, args={"o": oid})


async def count_orders(conn: Connection, db, args: dict) -> dict:
    filters = _opt(args, "status", "financial_status", "fulfillment_status",
                   "created_at_min", "created_at_max")
    filters.setdefault("status", "any")

    async def _load():
        body, _ = await _get(conn, db, "orders/count.json", filters)
        return {"count": body.get("count"), "filters": filters}

    return await cached(SLUG, conn.id, "count_orders", TTL_SHORT, _load, args={"f": filters})


async def sales_summary(conn: Connection, db, args: dict) -> dict:
    """Revenue, order count and averages over the orders in one window.

    Summed from the orders actually fetched, and the response says how many
    that was -- a total over one page presented as a store total would be a
    wrong number stated confidently.
    """
    limit = _limit(args, 250, 250)
    filters = _opt(args, "created_at_min", "created_at_max", "financial_status")
    filters.setdefault("status", "any")

    async def _load():
        rows, nxt = await _paged(conn, db, "orders.json", "orders", dict(filters), limit)
        gross = sum(_money(o.get("total_price")) or 0.0 for o in rows)
        discounts = sum(_money(o.get("total_discounts")) or 0.0 for o in rows)
        tax = sum(_money(o.get("total_tax")) or 0.0 for o in rows)
        items = sum(
            sum(int(li.get("quantity") or 0) for li in (o.get("line_items") or []))
            for o in rows
        )
        currencies = sorted({o.get("currency") for o in rows if o.get("currency")})
        return {
            "orders_counted": len(rows),
            "complete": nxt == "",
            "note": "" if nxt == "" else
                    "More orders match than were counted -- this covers the first page only.",
            "currencies": currencies,
            "gross_sales": round(gross, 2),
            "total_discounts": round(discounts, 2),
            "total_tax": round(tax, 2),
            "items_sold": items,
            "average_order_value": round(gross / len(rows), 2) if rows else None,
            "filters": filters,
        }

    return await cached(SLUG, conn.id, "sales_summary", TTL_SHORT, _load,
                        args={"lim": limit, "f": filters})


async def top_products(conn: Connection, db, args: dict) -> dict:
    """Best sellers by units, rolled up from order line items in the window."""
    limit = _limit(args, 250, 250)
    top = _limit({"limit": (args or {}).get("top", 20)}, 20, 100)
    filters = _opt(args, "created_at_min", "created_at_max")
    filters.setdefault("status", "any")

    async def _load():
        rows, nxt = await _paged(conn, db, "orders.json", "orders", dict(filters), limit)
        tally: dict = {}
        for o in rows:
            for li in o.get("line_items") or []:
                key = li.get("product_id") or li.get("title")
                entry = tally.setdefault(key, {
                    "product_id": li.get("product_id"),
                    "title": li.get("title"),
                    "sku": li.get("sku"),
                    "units": 0,
                    "revenue": 0.0,
                })
                qty = int(li.get("quantity") or 0)
                entry["units"] += qty
                entry["revenue"] += (_money(li.get("price")) or 0.0) * qty
        ranked = sorted(tally.values(), key=lambda r: r["units"], reverse=True)[:top]
        for r in ranked:
            r["revenue"] = round(r["revenue"], 2)
        return {
            "orders_counted": len(rows),
            "complete": nxt == "",
            "row_count": len(ranked),
            "products": ranked,
        }

    return await cached(SLUG, conn.id, "top_products", TTL_SHORT, _load,
                        args={"lim": limit, "t": top, "f": filters})


async def list_abandoned_checkouts(conn: Connection, db, args: dict) -> dict:
    limit = _limit(args, 50, 250)
    filters = _opt(args, "created_at_min", "created_at_max", "page_info")

    async def _load():
        rows, nxt = await _paged(conn, db, "checkouts.json", "checkouts", dict(filters), limit)
        return {
            "row_count": len(rows),
            "next_page_info": nxt,
            "checkouts": [
                {
                    "id": c.get("id"),
                    "created_at": c.get("created_at"),
                    "updated_at": c.get("updated_at"),
                    "email": c.get("email"),
                    "currency": c.get("currency"),
                    "total_price": _money(c.get("total_price")),
                    "line_item_count": len(c.get("line_items", []) or []),
                    "abandoned_checkout_url": c.get("abandoned_checkout_url"),
                }
                for c in rows
            ],
        }

    return await cached(SLUG, conn.id, "list_abandoned_checkouts", TTL_SHORT, _load,
                        args={"lim": limit, "f": filters})


# =========================================================================== #
# Customers
# =========================================================================== #
async def list_customers(conn: Connection, db, args: dict) -> dict:
    limit = _limit(args, 50, 250)
    filters = _opt(args, "created_at_min", "updated_at_min", "since_id", "page_info")

    async def _load():
        rows, nxt = await _paged(conn, db, "customers.json", "customers", dict(filters), limit)
        return {
            "row_count": len(rows),
            "next_page_info": nxt,
            "customers": [_customer(c) for c in rows],
        }

    return await cached(SLUG, conn.id, "list_customers", TTL_SHORT, _load,
                        args={"lim": limit, "f": filters})


async def get_customer(conn: Connection, db, args: dict) -> dict:
    cid = _require(args, "customer_id", "6123456789")

    async def _load():
        body, _ = await _get(conn, db, f"customers/{cid}.json")
        return _customer(body.get("customer", {}) or {})

    return await cached(SLUG, conn.id, "get_customer", TTL_SHORT, _load, args={"c": cid})


async def search_customers(conn: Connection, db, args: dict) -> dict:
    """Shopify's own customer search: email, name, tag, and its query syntax."""
    query = _require(args, "query", "email:someone@example.com")
    limit = _limit(args, 50, 250)

    async def _load():
        body, _ = await _get(conn, db, "customers/search.json",
                             {"query": query, "limit": limit})
        rows = body.get("customers", []) or []
        return {"query": query, "row_count": len(rows),
                "customers": [_customer(c) for c in rows]}

    return await cached(SLUG, conn.id, "search_customers", TTL_SHORT, _load,
                        args={"q": query, "lim": limit})


async def count_customers(conn: Connection, db, args: dict) -> dict:
    async def _load():
        body, _ = await _get(conn, db, "customers/count.json")
        return {"count": body.get("count")}

    return await cached(SLUG, conn.id, "count_customers", TTL_SHORT, _load)


# =========================================================================== #
# Merchandising and inventory
# =========================================================================== #
async def list_collections(conn: Connection, db, args: dict) -> dict:
    """Custom and smart collections together -- Shopify keeps them apart, but
    to anyone merchandising the store they are one list."""
    limit = _limit(args, 50, 250)

    async def _load():
        custom, _ = await _paged(conn, db, "custom_collections.json",
                                 "custom_collections", {}, limit)
        smart, _ = await _paged(conn, db, "smart_collections.json",
                                "smart_collections", {}, limit)
        rows = [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "handle": c.get("handle"),
                "kind": kind,
                "published_at": c.get("published_at"),
                "updated_at": c.get("updated_at"),
                "products_count": c.get("products_count"),
            }
            for kind, group in (("custom", custom), ("smart", smart))
            for c in group
        ]
        return {"row_count": len(rows), "collections": rows}

    return await cached(SLUG, conn.id, "list_collections", TTL_MEDIUM, _load,
                        args={"lim": limit})


async def list_inventory(conn: Connection, db, args: dict) -> dict:
    """Stock on hand per inventory item and location."""
    ids = str((args or {}).get("inventory_item_ids") or "").strip()
    location = str((args or {}).get("location_id") or "").strip()
    if not ids and not location:
        raise ConnectorError(
            "Give inventory_item_ids (comma separated, from a product's "
            "variants) or a location_id. Shopify will not list all levels."
        )
    limit = _limit(args, 50, 250)
    params = {"limit": limit}
    if ids:
        params["inventory_item_ids"] = ids
    if location:
        params["location_ids"] = location

    async def _load():
        body, _ = await _get(conn, db, "inventory_levels.json", params)
        rows = body.get("inventory_levels", []) or []
        return {"row_count": len(rows), "levels": rows}

    return await cached(SLUG, conn.id, "list_inventory", TTL_SHORT, _load,
                        args={"i": ids, "l": location, "lim": limit})


async def list_locations(conn: Connection, db, args: dict) -> dict:
    async def _load():
        body, _ = await _get(conn, db, "locations.json")
        rows = body.get("locations", []) or []
        return {
            "row_count": len(rows),
            "locations": [
                {
                    "id": l.get("id"),
                    "name": l.get("name"),
                    "city": l.get("city"),
                    "country": l.get("country_name"),
                    "active": l.get("active"),
                }
                for l in rows
            ],
        }

    return await cached(SLUG, conn.id, "list_locations", TTL_MEDIUM, _load)


async def low_stock(conn: Connection, db, args: dict) -> dict:
    """Variants at or under a threshold, from the products page scanned."""
    try:
        threshold = int((args or {}).get("threshold", 5))
    except (TypeError, ValueError):
        threshold = 5
    limit = _limit(args, 250, 250)

    async def _load():
        rows, nxt = await _paged(conn, db, "products.json", "products", {}, limit)
        low = []
        for p in rows:
            for v in p.get("variants") or []:
                qty = v.get("inventory_quantity")
                if isinstance(qty, int) and qty <= threshold:
                    low.append({
                        "product_id": p.get("id"),
                        "product_title": p.get("title"),
                        "variant_id": v.get("id"),
                        "variant_title": v.get("title"),
                        "sku": v.get("sku"),
                        "inventory_quantity": qty,
                    })
        low.sort(key=lambda r: r["inventory_quantity"])
        return {
            "threshold": threshold,
            "products_scanned": len(rows),
            "complete": nxt == "",
            "row_count": len(low),
            "variants": low,
        }

    return await cached(SLUG, conn.id, "low_stock", TTL_SHORT, _load,
                        args={"t": threshold, "lim": limit})


async def list_price_rules(conn: Connection, db, args: dict) -> dict:
    """Discounts, as the price rules behind them."""
    limit = _limit(args, 50, 250)

    async def _load():
        rows, nxt = await _paged(conn, db, "price_rules.json", "price_rules", {}, limit)
        return {
            "row_count": len(rows),
            "next_page_info": nxt,
            "price_rules": [
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "value_type": r.get("value_type"),
                    "value": _money(r.get("value")),
                    "target_type": r.get("target_type"),
                    "allocation_method": r.get("allocation_method"),
                    "starts_at": r.get("starts_at"),
                    "ends_at": r.get("ends_at"),
                    "usage_limit": r.get("usage_limit"),
                    "once_per_customer": r.get("once_per_customer"),
                }
                for r in rows
            ],
        }

    return await cached(SLUG, conn.id, "list_price_rules", TTL_MEDIUM, _load,
                        args={"lim": limit})


# =========================================================================== #
# Catalog
# =========================================================================== #
_LIMIT = {"type": "integer", "description": "Rows to return (1-250). Default 50."}
_PAGE = {"type": "string", "description": "next_page_info from a previous call. Filters are ignored when this is set -- Shopify rejects the combination."}
_FROM = {"type": "string", "description": "ISO 8601 lower bound, e.g. '2026-01-01T00:00:00Z'."}
_TO = {"type": "string", "description": "ISO 8601 upper bound."}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


CATALOG = {
    "shop_info": {
        "description": "The store itself: name, plan, currency, timezone and domains.",
        "input": _obj({}),
    },
    "list_products": {
        "description": "Products with their variants, newest first.",
        "input": _obj({
            "status": {"type": "string", "description": "active, archived or draft."},
            "vendor": {"type": "string"},
            "product_type": {"type": "string"},
            "collection_id": {"type": "string"},
            "created_at_min": _FROM,
            "updated_at_min": _FROM,
            "limit": _LIMIT,
            "page_info": _PAGE,
        }),
    },
    "get_product": {
        "description": "One product in full: variants, images, options and description.",
        "input": _obj({"product_id": {"type": "string"}}, ["product_id"]),
    },
    "search_products": {
        "description": "Match product titles within the scanned page. Shopify's REST product list has no server-side text search.",
        "input": _obj({
            "query": {"type": "string", "description": "Case-insensitive substring of the title."},
            "limit": _LIMIT,
        }, ["query"]),
    },
    "count_products": {
        "description": "How many products match, without fetching them.",
        "input": _obj({
            "status": {"type": "string"}, "vendor": {"type": "string"},
            "product_type": {"type": "string"}, "collection_id": {"type": "string"},
        }),
    },
    "list_orders": {
        "description": "Orders, newest first. Defaults to every status, not just open ones.",
        "input": _obj({
            "status": {"type": "string", "description": "open, closed, cancelled or any. Default any."},
            "financial_status": {"type": "string", "description": "paid, pending, refunded, ..."},
            "fulfillment_status": {"type": "string", "description": "shipped, unshipped, partial, ..."},
            "created_at_min": _FROM, "created_at_max": _TO, "updated_at_min": _FROM,
            "since_id": {"type": "string"}, "limit": _LIMIT, "page_info": _PAGE,
        }),
    },
    "get_order": {
        "description": "One order in full: line items, shipping, discounts and refund count.",
        "input": _obj({"order_id": {"type": "string"}}, ["order_id"]),
    },
    "count_orders": {
        "description": "How many orders match, without fetching them.",
        "input": _obj({
            "status": {"type": "string"}, "financial_status": {"type": "string"},
            "fulfillment_status": {"type": "string"},
            "created_at_min": _FROM, "created_at_max": _TO,
        }),
    },
    "sales_summary": {
        "description": "Gross sales, discounts, tax, units and AOV over a window. Says how many orders it counted and whether more matched.",
        "input": _obj({
            "created_at_min": _FROM, "created_at_max": _TO,
            "financial_status": {"type": "string"}, "limit": _LIMIT,
        }),
    },
    "top_products": {
        "description": "Best sellers by units, rolled up from order line items in the window.",
        "input": _obj({
            "created_at_min": _FROM, "created_at_max": _TO,
            "top": {"type": "integer", "description": "How many to return (1-100). Default 20."},
            "limit": _LIMIT,
        }),
    },
    "list_abandoned_checkouts": {
        "description": "Carts that were never completed, with their recovery URLs.",
        "input": _obj({"created_at_min": _FROM, "created_at_max": _TO,
                       "limit": _LIMIT, "page_info": _PAGE}),
    },
    "list_customers": {
        "description": "Customers with order counts and lifetime spend.",
        "input": _obj({"created_at_min": _FROM, "updated_at_min": _FROM,
                       "since_id": {"type": "string"}, "limit": _LIMIT, "page_info": _PAGE}),
    },
    "get_customer": {
        "description": "One customer.",
        "input": _obj({"customer_id": {"type": "string"}}, ["customer_id"]),
    },
    "search_customers": {
        "description": "Shopify's customer search, using its own query syntax (email:, first_name:, tag:).",
        "input": _obj({"query": {"type": "string"}, "limit": _LIMIT}, ["query"]),
    },
    "count_customers": {
        "description": "Total customers on the store.",
        "input": _obj({}),
    },
    "list_collections": {
        "description": "Custom and smart collections together, each tagged with which kind it is.",
        "input": _obj({"limit": _LIMIT}),
    },
    "list_inventory": {
        "description": "Stock levels per inventory item and location. Needs inventory_item_ids or a location_id.",
        "input": _obj({
            "inventory_item_ids": {"type": "string", "description": "Comma separated, from a product's variants."},
            "location_id": {"type": "string"},
            "limit": _LIMIT,
        }),
    },
    "list_locations": {
        "description": "Warehouses and stores stock is held at.",
        "input": _obj({}),
    },
    "low_stock": {
        "description": "Variants at or below a stock threshold, from the products scanned.",
        "input": _obj({
            "threshold": {"type": "integer", "description": "Inclusive. Default 5."},
            "limit": _LIMIT,
        }),
    },
    "list_price_rules": {
        "description": "Discounts, as the price rules behind them.",
        "input": _obj({"limit": _LIMIT}),
    },
}

HANDLERS = {
    "shop_info": shop_info,
    "list_products": list_products,
    "get_product": get_product,
    "search_products": search_products,
    "count_products": count_products,
    "list_orders": list_orders,
    "get_order": get_order,
    "count_orders": count_orders,
    "sales_summary": sales_summary,
    "top_products": top_products,
    "list_abandoned_checkouts": list_abandoned_checkouts,
    "list_customers": list_customers,
    "get_customer": get_customer,
    "search_customers": search_customers,
    "count_customers": count_customers,
    "list_collections": list_collections,
    "list_inventory": list_inventory,
    "list_locations": list_locations,
    "low_stock": low_stock,
    "list_price_rules": list_price_rules,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Shopify",
        auth="api_key",
        description=(
            "Reads a Shopify store through the Admin API: products and variants, "
            "orders and line items, customers, collections, inventory, discounts, "
            "abandoned checkouts, and rolled-up sales and best sellers."
        ),
        category="Commerce",
        cred_fields=["shop_domain", "access_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
