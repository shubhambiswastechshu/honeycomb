"""Salesforce B2C Commerce Cloud connector (SCAPI shopper APIs, api_key).

Reads a B2C Commerce storefront: product search with facets, product detail
with variants and availability, the category tree, search suggestions, and
active promotions.

Auth is the SCAPI client-credentials flow. The connection stores four
non-secret identifiers plus one secret:

  short_code     the tenant's API short code, e.g. 'kv7kzm78'
  organization_id the realm/instance id, e.g. 'f_ecom_zzrf_001'
  site_id        the storefront, e.g. 'RefArch'
  client_id      the SLAS client
  client_secret  its secret

`_token` exchanges the client id and secret for a short-lived shopper access
token on each call, the same way the Google connectors refresh. The token is
never stored.

**short_code and organization_id are caller-supplied and go into the request
URL**, which is an SSRF hole if taken at face value -- a "short code" of
``169.254.169.254`` would aim this server's HTTP client at the cloud metadata
endpoint. Both are validated against a strict character class before any URL is
built; see `_short_code` and `_org`.

Everything here is a shopper API, which is read-only by construction: these are
the endpoints a storefront calls to render pages. No catalog entry carries
``write``.
"""
import re

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post

SLUG = "salesforce_commerce"

# Tenant short codes are lowercase alphanumeric; realm ids look like
# 'f_ecom_zzrf_001'. Both land in the hostname or path, so both are pinned to
# a character class that cannot express a host, a scheme or a traversal.
_SHORT_RE = re.compile(r"^[a-z0-9]{4,16}$")
_ORG_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_SITE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# --------------------------------------------------------------------------- #
# Credentials, validated
# --------------------------------------------------------------------------- #
def _cred(conn: Connection, key: str) -> str:
    value = str(conn.creds().get(key) or "").strip()
    if not value:
        raise ConnectorError(f"Not connected: missing {key}.")
    return value


def _short_code(conn: Connection) -> str:
    code = _cred(conn, "short_code").lower()
    if not _SHORT_RE.match(code):
        raise ConnectorError(
            "short_code must be the tenant's API short code -- lowercase "
            "letters and digits only, e.g. 'kv7kzm78'."
        )
    return code


def _org(conn: Connection) -> str:
    org = _cred(conn, "organization_id")
    if not _ORG_RE.match(org):
        raise ConnectorError(
            "organization_id must look like 'f_ecom_zzrf_001' -- letters, "
            "digits, underscores and hyphens only."
        )
    return org


def _site(conn: Connection, args: dict) -> str:
    site = str((args or {}).get("site_id") or "").strip() or _cred(conn, "site_id")
    if not _SITE_RE.match(site):
        raise ConnectorError("site_id must be a storefront id, e.g. 'RefArch'.")
    return site


def _host(conn: Connection) -> str:
    return f"https://{_short_code(conn)}.api.commercecloud.salesforce.com"


def _ident(value: str, what: str) -> str:
    """A product, category or promotion id destined for a URL path."""
    v = str(value or "").strip()
    if not _ID_RE.match(v):
        raise ConnectorError(
            f"{what} must be a plain id -- letters, digits, dots, underscores "
            f"and hyphens only. Got {v[:60]!r}."
        )
    return v


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _fail(res) -> None:
    try:
        body = res.json() or {}
        message = body.get("detail") or body.get("message") or body.get("title") or res.text[:300]
    except ValueError:
        message = res.text[:300]
    if res.status_code in (401, 403):
        raise ConnectorError(
            f"Commerce Cloud refused this call ({res.status_code}) -- check the "
            f"SLAS client's scopes and that it is enabled for this site. "
            f"{str(message)[:200]}"
        )
    if res.status_code == 404:
        raise ConnectorError(f"Commerce Cloud: not found. {str(message)[:200]}")
    if res.status_code == 429:
        raise ConnectorError("Commerce Cloud rate limit hit (429). Try again shortly.")
    raise ConnectorError(f"Commerce Cloud {res.status_code}: {str(message)[:300]}")


async def _token(conn: Connection, db) -> str:
    """Client-credentials exchange for a shopper access token."""
    url = f"{_host(conn)}/shopper/auth/v1/organizations/{_org(conn)}/oauth2/token"
    async with limit_for(url):
        try:
            res = await http_post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(_cred(conn, "client_id"), _cred(conn, "client_secret")),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        # Never echo the body here -- a failed token exchange can reflect the
        # credential back in its error payload.
        raise ConnectorError(
            f"Commerce Cloud token exchange failed ({res.status_code}). Check "
            "client_id, client_secret, short_code and organization_id."
        )
    token = (res.json() or {}).get("access_token")
    if not token:
        raise ConnectorError("Commerce Cloud returned no access_token.")
    return token


async def _get(conn: Connection, db, path: str, params: dict | None = None) -> dict:
    token = await _token(conn, db)
    url = f"{_host(conn)}/{path.lstrip('/')}"
    async with limit_for(url):
        try:
            res = await http_get(
                url,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                params=params or {},
            )
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


# --------------------------------------------------------------------------- #
# Arguments and shaping
# --------------------------------------------------------------------------- #
def _limit(args: dict, default: int = 25, max_value: int = 200) -> int:
    try:
        return max(1, min(int((args or {}).get("limit", default)), max_value))
    except (TypeError, ValueError):
        return default


def _offset(args: dict) -> int:
    try:
        return max(0, int((args or {}).get("offset", 0)))
    except (TypeError, ValueError):
        return 0


def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _price(hit: dict) -> dict:
    return {
        "price": hit.get("price"),
        "price_max": hit.get("priceMax"),
        "currency": hit.get("currency"),
        "price_per_unit": hit.get("pricePerUnit"),
    }


def _hit(h: dict) -> dict:
    return {
        "product_id": h.get("productId"),
        "product_name": h.get("productName"),
        "product_type": h.get("productType"),
        "represented_product": (h.get("representedProduct") or {}).get("id"),
        "orderable": h.get("orderable"),
        "image": (h.get("image") or {}).get("link"),
        **_price(h),
    }


def _category(c: dict, with_children: bool = False) -> dict:
    out = {
        "id": c.get("id"),
        "name": c.get("name"),
        "description": c.get("description"),
        "page_title": c.get("pageTitle"),
        "online": c.get("online"),
        "parent_category_id": c.get("parentCategoryId"),
        "child_count": len(c.get("categories", []) or []),
    }
    if with_children:
        out["categories"] = [_category(k) for k in (c.get("categories") or [])]
    return out


# =========================================================================== #
# Search
# =========================================================================== #
async def search_products(conn: Connection, db, args: dict) -> dict:
    """Storefront product search, with the facets the search returned."""
    query = _require(args, "query", "shirt")
    site = _site(conn, args)
    limit = _limit(args, 25, 200)
    offset = _offset(args)
    params = {"siteId": site, "q": query, "limit": limit, "offset": offset}
    refine = str((args or {}).get("refine") or "").strip()
    if refine:
        # SCAPI takes refinements as repeated refine params, e.g. 'cgid=mens'.
        params["refine"] = refine
    sort = str((args or {}).get("sort") or "").strip()
    if sort:
        params["sort"] = sort

    async def _load():
        body = await _get(
            conn, db,
            f"search/shopper-search/v1/organizations/{_org(conn)}/product-search",
            params,
        )
        hits = [_hit(h) for h in body.get("hits", []) or []]
        facets = [
            {
                "attribute_id": r.get("attributeId"),
                "label": r.get("label"),
                "values": [
                    {"label": v.get("label"), "value": v.get("value"), "hits": v.get("hitCount")}
                    for v in (r.get("values") or [])[:40]
                ],
            }
            for r in body.get("refinements", []) or []
        ]
        return {
            "query": query,
            "site_id": site,
            "total": body.get("total"),
            "offset": body.get("offset"),
            "row_count": len(hits),
            "sorting_options": [
                {"id": s.get("id"), "label": s.get("label")}
                for s in body.get("sortingOptions", []) or []
            ],
            "refinements": facets,
            "hits": hits,
        }

    return await cached(SLUG, conn.id, "search_products", TTL_SHORT, _load,
                        args={"q": query, "s": site, "l": limit, "o": offset,
                              "r": refine, "so": sort})


async def search_suggestions(conn: Connection, db, args: dict) -> dict:
    """Type-ahead suggestions: products, categories and brands for a prefix."""
    query = _require(args, "query", "shi")
    site = _site(conn, args)
    limit = _limit(args, 10, 50)

    async def _load():
        body = await _get(
            conn, db,
            f"search/shopper-search/v1/organizations/{_org(conn)}/search-suggestions",
            {"siteId": site, "q": query, "limit": limit},
        )
        return {
            "query": query,
            "site_id": site,
            "products": [
                {"id": s.get("productId"), "name": s.get("productName")}
                for s in ((body.get("productSuggestions") or {}).get("products") or [])
            ],
            "categories": [
                {"id": s.get("id"), "name": s.get("name")}
                for s in ((body.get("categorySuggestions") or {}).get("categories") or [])
            ],
            "brands": (body.get("brandSuggestions") or {}).get("brands") or [],
        }

    return await cached(SLUG, conn.id, "search_suggestions", TTL_SHORT, _load,
                        args={"q": query, "s": site, "l": limit})


# =========================================================================== #
# Products
# =========================================================================== #
async def get_product(conn: Connection, db, args: dict) -> dict:
    """One product in full: variants, options, inventory and prices."""
    pid = _ident(_require(args, "product_id", "25592770M"), "product_id")
    site = _site(conn, args)

    async def _load():
        body = await _get(
            conn, db,
            f"product/shopper-products/v1/organizations/{_org(conn)}/products/{pid}",
            {"siteId": site, "allImages": "false",
             "expand": "availability,prices,images,variations,promotions"},
        )
        inv = body.get("inventory") or {}
        return {
            "id": body.get("id"),
            "name": body.get("name"),
            "site_id": site,
            "short_description": (body.get("shortDescription") or "")[:2000],
            "long_description": (body.get("longDescription") or "")[:4000],
            "brand": body.get("brand"),
            "type": body.get("type"),
            "currency": body.get("currency"),
            "price": body.get("price"),
            "price_max": body.get("priceMax"),
            "primary_category_id": body.get("primaryCategoryId"),
            "orderable": inv.get("orderable"),
            "stock_level": inv.get("stockLevel"),
            "ats": inv.get("ats"),
            "variation_attributes": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "values": [
                        {"value": v.get("value"), "name": v.get("name"),
                         "orderable": v.get("orderable")}
                        for v in (a.get("values") or [])[:60]
                    ],
                }
                for a in (body.get("variationAttributes") or [])
            ],
            "variant_count": len(body.get("variants", []) or []),
            "variants": [
                {
                    "product_id": v.get("productId"),
                    "orderable": v.get("orderable"),
                    "price": v.get("price"),
                    "variation_values": v.get("variationValues"),
                }
                for v in (body.get("variants") or [])[:100]
            ],
            "image_groups": len(body.get("imageGroups", []) or []),
        }

    return await cached(SLUG, conn.id, "get_product", TTL_SHORT, _load,
                        args={"p": pid, "s": site})


async def get_products(conn: Connection, db, args: dict) -> dict:
    """Several products at once. SCAPI takes up to 24 ids per call."""
    raw = _require(args, "product_ids", "25592770M,25592771M")
    site = _site(conn, args)
    ids = [_ident(p, "product_ids") for p in
           [x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()]][:24]
    if not ids:
        raise ConnectorError("product_ids listed no usable ids.")

    async def _load():
        body = await _get(
            conn, db,
            f"product/shopper-products/v1/organizations/{_org(conn)}/products",
            {"siteId": site, "ids": ",".join(ids), "expand": "availability,prices"},
        )
        rows = [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "brand": d.get("brand"),
                "price": d.get("price"),
                "currency": d.get("currency"),
                "orderable": (d.get("inventory") or {}).get("orderable"),
                "stock_level": (d.get("inventory") or {}).get("stockLevel"),
            }
            for d in body.get("data", []) or []
        ]
        return {"site_id": site, "requested": len(ids),
                "row_count": len(rows), "products": rows}

    return await cached(SLUG, conn.id, "get_products", TTL_SHORT, _load,
                        args={"i": ids, "s": site})


# =========================================================================== #
# Categories
# =========================================================================== #
async def list_categories(conn: Connection, db, args: dict) -> dict:
    """Top-level categories, or the children of one."""
    site = _site(conn, args)
    root = str((args or {}).get("category_id") or "root").strip()
    root = _ident(root, "category_id")
    try:
        levels = max(1, min(int((args or {}).get("levels", 1)), 2))
    except (TypeError, ValueError):
        levels = 1

    async def _load():
        body = await _get(
            conn, db,
            f"product/shopper-products/v1/organizations/{_org(conn)}/categories/{root}",
            {"siteId": site, "levels": levels},
        )
        kids = body.get("categories", []) or []
        return {
            "site_id": site,
            "category_id": body.get("id"),
            "name": body.get("name"),
            "row_count": len(kids),
            "categories": [_category(c, with_children=levels > 1) for c in kids],
        }

    return await cached(SLUG, conn.id, "list_categories", TTL_MEDIUM, _load,
                        args={"s": site, "c": root, "l": levels})


async def get_category(conn: Connection, db, args: dict) -> dict:
    """One category with its immediate children."""
    site = _site(conn, args)
    cid = _ident(_require(args, "category_id", "mens"), "category_id")

    async def _load():
        body = await _get(
            conn, db,
            f"product/shopper-products/v1/organizations/{_org(conn)}/categories/{cid}",
            {"siteId": site, "levels": 1},
        )
        return {"site_id": site, **_category(body, with_children=True)}

    return await cached(SLUG, conn.id, "get_category", TTL_MEDIUM, _load,
                        args={"s": site, "c": cid})


# =========================================================================== #
# Promotions
# =========================================================================== #
async def list_promotions(conn: Connection, db, args: dict) -> dict:
    """Active promotions by id. SCAPI has no list-all, so ids are required."""
    site = _site(conn, args)
    raw = _require(args, "promotion_ids", "10off,freeship")
    ids = [_ident(p, "promotion_ids") for p in
           [x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()]][:50]

    async def _load():
        body = await _get(
            conn, db,
            f"pricing/shopper-promotions/v1/organizations/{_org(conn)}/promotions",
            {"siteId": site, "ids": ",".join(ids)},
        )
        rows = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "calloutMsg": p.get("calloutMsg"),
                "details": (p.get("details") or "")[:1000],
                "currency": p.get("currency"),
            }
            for p in body.get("data", []) or []
        ]
        return {"site_id": site, "requested": len(ids),
                "row_count": len(rows), "promotions": rows}

    return await cached(SLUG, conn.id, "list_promotions", TTL_MEDIUM, _load,
                        args={"s": site, "i": ids})


# =========================================================================== #
# Catalog
# =========================================================================== #
_SITE = {"type": "string", "description": "Storefront id. Optional -- the connection's saved site_id is used when omitted."}
_LIMIT = {"type": "integer", "description": "Rows to return (1-200). Default 25."}
_OFFSET = {"type": "integer", "description": "Rows to skip, for paging. Default 0."}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


CATALOG = {
    "search_products": {
        "description": (
            "Storefront product search. Returns hits, the total, the sorting "
            "options and the refinement facets the search produced."
        ),
        "input": _obj({
            "query": {"type": "string", "description": "Search text."},
            "refine": {"type": "string", "description": "A refinement, e.g. 'cgid=mens' or 'price=(0..50)'."},
            "sort": {"type": "string", "description": "A sorting option id from a previous search."},
            "site_id": _SITE, "limit": _LIMIT, "offset": _OFFSET,
        }, ["query"]),
    },
    "search_suggestions": {
        "description": "Type-ahead suggestions for a prefix: products, categories and brands.",
        "input": _obj({
            "query": {"type": "string", "description": "Partial search text, two characters or more."},
            "site_id": _SITE,
            "limit": {"type": "integer", "description": "Suggestions per group (1-50). Default 10."},
        }, ["query"]),
    },
    "get_product": {
        "description": "One product in full: description, prices, variation attributes, variants and stock.",
        "input": _obj({
            "product_id": {"type": "string", "description": "Product id, e.g. '25592770M'."},
            "site_id": _SITE,
        }, ["product_id"]),
    },
    "get_products": {
        "description": "Several products at once, with price and availability. Up to 24 ids per call.",
        "input": _obj({
            "product_ids": {"type": "string", "description": "Comma separated product ids."},
            "site_id": _SITE,
        }, ["product_ids"]),
    },
    "list_categories": {
        "description": "Top-level categories, or the children of one. Start with the default 'root'.",
        "input": _obj({
            "category_id": {"type": "string", "description": "Parent to list under. Default 'root'."},
            "levels": {"type": "integer", "description": "How deep to expand (1-2). Default 1."},
            "site_id": _SITE,
        }),
    },
    "get_category": {
        "description": "One category and its immediate children.",
        "input": _obj({
            "category_id": {"type": "string", "description": "Category id, e.g. 'mens'."},
            "site_id": _SITE,
        }, ["category_id"]),
    },
    "list_promotions": {
        "description": "Promotions by id, with their callout messages. Commerce Cloud has no list-all, so ids are required.",
        "input": _obj({
            "promotion_ids": {"type": "string", "description": "Comma separated promotion ids."},
            "site_id": _SITE,
        }, ["promotion_ids"]),
    },
}

HANDLERS = {
    "search_products": search_products,
    "search_suggestions": search_suggestions,
    "get_product": get_product,
    "get_products": get_products,
    "list_categories": list_categories,
    "get_category": get_category,
    "list_promotions": list_promotions,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Salesforce B2C Commerce",
        auth="api_key",
        description=(
            "Reads a Salesforce B2C Commerce storefront through SCAPI: product "
            "search with facets, product detail with variants and stock, the "
            "category tree, search suggestions and promotions."
        ),
        category="Commerce",
        cred_fields=[
            "short_code", "organization_id", "site_id", "client_id", "client_secret",
        ],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
