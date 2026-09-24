"""HubSpot CRM connector (CRM API v3, api_key).

Reads the CRM: contacts, companies, deals and tickets, the pipelines deals move
through, owners, notes, and the property definitions behind every object.

Auth is a HubSpot private app access token sent as a bearer token. Give the app
only the ``crm.objects.*.read`` and ``crm.schemas.*.read`` scopes -- nothing
here writes, every catalog entry is read-only, and the registry derives an
empty ``write_tools``.

One thing looks wrong and is not: **the search tools issue POST requests.**
HubSpot's search endpoint takes its filters in a JSON body, so POST is the only
way to call it. It creates nothing; `_search` exists as a separate helper from
`_get` precisely so that the one POST path in this file is obvious and can be
seen to be a read.

HubSpot returns object fields inside a ``properties`` bag and, by default,
returns barely any of them. Each tool therefore asks for the properties it
intends to show -- see the ``_*_PROPS`` tuples -- rather than accepting the
default and quietly reporting nulls for fields that were simply never
requested.
"""
from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post

SLUG = "hubspot"
BASE = "https://api.hubapi.com"

_CONTACT_PROPS = (
    "firstname", "lastname", "email", "phone", "company", "jobtitle",
    "lifecyclestage", "hs_lead_status", "createdate", "lastmodifieddate",
    "hubspot_owner_id", "city", "country", "website",
)
_COMPANY_PROPS = (
    "name", "domain", "industry", "city", "country", "numberofemployees",
    "annualrevenue", "lifecyclestage", "createdate", "hs_lastmodifieddate",
    "hubspot_owner_id", "phone", "type",
)
_DEAL_PROPS = (
    "dealname", "amount", "dealstage", "pipeline", "closedate", "createdate",
    "hs_lastmodifieddate", "hubspot_owner_id", "dealtype", "hs_is_closed_won",
    "hs_is_closed", "hs_deal_stage_probability",
)
_TICKET_PROPS = (
    "subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority",
    "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _headers(conn: Connection) -> dict:
    token = str(conn.creds().get("access_token") or "").strip()
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def _fail(res) -> None:
    try:
        body = res.json() or {}
        message = body.get("message") or res.text[:300]
    except ValueError:
        message = res.text[:300]
    if res.status_code == 401:
        raise ConnectorError("HubSpot rejected the access token (401).")
    if res.status_code == 403:
        raise ConnectorError(
            "HubSpot refused this call (403) -- the private app is probably "
            f"missing a read scope. {str(message)[:200]}"
        )
    if res.status_code == 429:
        raise ConnectorError("HubSpot rate limit hit (429). Try again shortly.")
    raise ConnectorError(f"HubSpot {res.status_code}: {str(message)[:300]}")


async def _get(conn: Connection, db, path: str, params: dict | None = None) -> dict:
    url = BASE + "/" + path.lstrip("/")
    async with limit_for(BASE):
        try:
            res = await http_get(url, headers=_headers(conn), params=params or {})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


async def _search(conn: Connection, db, obj: str, body: dict) -> dict:
    """HubSpot's search endpoint. POST because the filters travel as JSON.

    This reads. It is a separate function from _get so the single POST in this
    module is visible rather than buried in a method argument.
    """
    url = f"{BASE}/crm/v3/objects/{obj}/search"
    async with limit_for(BASE):
        try:
            res = await http_post(url, headers=_headers(conn), json=body)
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


# --------------------------------------------------------------------------- #
# Arguments and shaping
# --------------------------------------------------------------------------- #
def _limit(args: dict, default: int = 50) -> int:
    """HubSpot caps list pages at 100 and search pages at 200; 100 is safe for both."""
    try:
        return max(1, min(int((args or {}).get("limit", default)), 100))
    except (TypeError, ValueError):
        return default


def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _after(args: dict) -> str:
    return str((args or {}).get("after") or "").strip()


def _flat(row: dict) -> dict:
    """Lift HubSpot's `properties` bag up to the top level.

    Every object comes back as {id, properties: {...}, createdAt, ...}, which
    makes every downstream read two levels deep for no reason. The id and
    timestamps stay where they are; the properties come up beside them.
    """
    out = {"id": row.get("id")}
    out.update(row.get("properties") or {})
    if row.get("archived") is not None:
        out["archived"] = row.get("archived")
    return out


def _envelope(body: dict, key: str) -> dict:
    rows = [_flat(r) for r in body.get("results", []) or []]
    nxt = ((body.get("paging") or {}).get("next") or {}).get("after") or ""
    out = {"row_count": len(rows), "next_after": nxt, key: rows}
    if body.get("total") is not None:
        out["total"] = body.get("total")
    return out


async def _list(conn: Connection, db, obj: str, props: tuple, args: dict, key: str) -> dict:
    limit = _limit(args)
    after = _after(args)
    params = {"limit": limit, "properties": ",".join(props)}
    if after:
        params["after"] = after

    async def _load():
        body = await _get(conn, db, f"crm/v3/objects/{obj}", params)
        return _envelope(body, key)

    return await cached(SLUG, conn.id, "list_" + obj, TTL_SHORT, _load,
                        args={"lim": limit, "a": after})


async def _text_search(conn: Connection, db, obj: str, props: tuple,
                       args: dict, key: str) -> dict:
    """HubSpot's cross-property free-text search for one object type."""
    query = _require(args, "query", "acme")
    limit = _limit(args)
    after = _after(args)
    body = {"query": query, "limit": limit, "properties": list(props)}
    if after:
        body["after"] = after

    async def _load():
        found = await _search(conn, db, obj, body)
        out = _envelope(found, key)
        out["query"] = query
        return out

    return await cached(SLUG, conn.id, "search_" + obj, TTL_SHORT, _load,
                        args={"q": query, "lim": limit, "a": after})


# =========================================================================== #
# Account
# =========================================================================== #
async def account_info(conn: Connection, db, args: dict) -> dict:
    """Which HubSpot portal this token belongs to."""

    async def _load():
        a = await _get(conn, db, "account-info/v3/details")
        return {
            "portal_id": a.get("portalId"),
            "account_type": a.get("accountType"),
            "time_zone": a.get("timeZone"),
            "company_currency": a.get("companyCurrency"),
            "ui_domain": a.get("uiDomain"),
            "data_hosting_location": a.get("dataHostingLocation"),
        }

    return await cached(SLUG, conn.id, "account_info", TTL_LONG, _load)


# =========================================================================== #
# Contacts
# =========================================================================== #
async def list_contacts(conn: Connection, db, args: dict) -> dict:
    return await _list(conn, db, "contacts", _CONTACT_PROPS, args, "contacts")


async def get_contact(conn: Connection, db, args: dict) -> dict:
    cid = _require(args, "contact_id", "12345")

    async def _load():
        body = await _get(conn, db, f"crm/v3/objects/contacts/{cid}",
                          {"properties": ",".join(_CONTACT_PROPS)})
        return _flat(body)

    return await cached(SLUG, conn.id, "get_contact", TTL_SHORT, _load, args={"c": cid})


async def search_contacts(conn: Connection, db, args: dict) -> dict:
    return await _text_search(conn, db, "contacts", _CONTACT_PROPS, args, "contacts")


# =========================================================================== #
# Companies
# =========================================================================== #
async def list_companies(conn: Connection, db, args: dict) -> dict:
    return await _list(conn, db, "companies", _COMPANY_PROPS, args, "companies")


async def get_company(conn: Connection, db, args: dict) -> dict:
    cid = _require(args, "company_id", "67890")

    async def _load():
        body = await _get(conn, db, f"crm/v3/objects/companies/{cid}",
                          {"properties": ",".join(_COMPANY_PROPS)})
        return _flat(body)

    return await cached(SLUG, conn.id, "get_company", TTL_SHORT, _load, args={"c": cid})


async def search_companies(conn: Connection, db, args: dict) -> dict:
    return await _text_search(conn, db, "companies", _COMPANY_PROPS, args, "companies")


# =========================================================================== #
# Deals
# =========================================================================== #
async def list_deals(conn: Connection, db, args: dict) -> dict:
    return await _list(conn, db, "deals", _DEAL_PROPS, args, "deals")


async def get_deal(conn: Connection, db, args: dict) -> dict:
    did = _require(args, "deal_id", "24680")

    async def _load():
        body = await _get(conn, db, f"crm/v3/objects/deals/{did}",
                          {"properties": ",".join(_DEAL_PROPS)})
        return _flat(body)

    return await cached(SLUG, conn.id, "get_deal", TTL_SHORT, _load, args={"d": did})


async def search_deals(conn: Connection, db, args: dict) -> dict:
    return await _text_search(conn, db, "deals", _DEAL_PROPS, args, "deals")


async def deals_by_stage(conn: Connection, db, args: dict) -> dict:
    """Open pipeline value grouped by stage, from the deals on one page.

    Says how many deals it counted and whether more matched, because a
    pipeline total computed over the first hundred deals and presented as the
    pipeline is exactly the kind of number people act on.
    """
    limit = _limit(args, 100)
    pipeline = str((args or {}).get("pipeline_id") or "").strip()
    params = {"limit": limit, "properties": ",".join(_DEAL_PROPS)}

    async def _load():
        body = await _get(conn, db, "crm/v3/objects/deals", params)
        rows = [_flat(r) for r in body.get("results", []) or []]
        if pipeline:
            rows = [r for r in rows if r.get("pipeline") == pipeline]
        stages: dict = {}
        for d in rows:
            key = d.get("dealstage") or "(no stage)"
            slot = stages.setdefault(key, {"dealstage": key, "deals": 0, "amount": 0.0})
            slot["deals"] += 1
            try:
                slot["amount"] += float(d.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        ranked = sorted(stages.values(), key=lambda s: s["amount"], reverse=True)
        for s in ranked:
            s["amount"] = round(s["amount"], 2)
        nxt = ((body.get("paging") or {}).get("next") or {}).get("after") or ""
        return {
            "deals_counted": len(rows),
            "complete": nxt == "",
            "note": "" if nxt == "" else
                    "More deals exist than were counted -- this covers the first page only.",
            "pipeline_id": pipeline,
            "total_amount": round(sum(s["amount"] for s in ranked), 2),
            "row_count": len(ranked),
            "by_stage": ranked,
        }

    return await cached(SLUG, conn.id, "deals_by_stage", TTL_SHORT, _load,
                        args={"lim": limit, "p": pipeline})


async def list_pipelines(conn: Connection, db, args: dict) -> dict:
    """Deal (or ticket) pipelines and the stages inside them."""
    obj = str((args or {}).get("object_type") or "deals").strip().lower()
    if obj not in ("deals", "tickets"):
        raise ConnectorError("object_type must be 'deals' or 'tickets'.")

    async def _load():
        body = await _get(conn, db, f"crm/v3/pipelines/{obj}")
        rows = [
            {
                "id": p.get("id"),
                "label": p.get("label"),
                "display_order": p.get("displayOrder"),
                "stages": [
                    {
                        "id": s.get("id"),
                        "label": s.get("label"),
                        "display_order": s.get("displayOrder"),
                        "probability": (s.get("metadata") or {}).get("probability"),
                        "closed_won": (s.get("metadata") or {}).get("isClosed"),
                    }
                    for s in sorted(p.get("stages", []) or [],
                                    key=lambda s: s.get("displayOrder") or 0)
                ],
            }
            for p in body.get("results", []) or []
        ]
        return {"object_type": obj, "row_count": len(rows), "pipelines": rows}

    return await cached(SLUG, conn.id, "list_pipelines", TTL_MEDIUM, _load, args={"o": obj})


# =========================================================================== #
# Tickets, owners, notes, schema
# =========================================================================== #
async def list_tickets(conn: Connection, db, args: dict) -> dict:
    return await _list(conn, db, "tickets", _TICKET_PROPS, args, "tickets")


async def list_owners(conn: Connection, db, args: dict) -> dict:
    """The people records can be assigned to. Joins hubspot_owner_id to a name."""
    limit = _limit(args)

    async def _load():
        body = await _get(conn, db, "crm/v3/owners", {"limit": limit})
        rows = [
            {
                "id": o.get("id"), "user_id": o.get("userId"),
                "email": o.get("email"),
                "first_name": o.get("firstName"), "last_name": o.get("lastName"),
                "archived": o.get("archived"),
            }
            for o in body.get("results", []) or []
        ]
        nxt = ((body.get("paging") or {}).get("next") or {}).get("after") or ""
        return {"row_count": len(rows), "next_after": nxt, "owners": rows}

    return await cached(SLUG, conn.id, "list_owners", TTL_MEDIUM, _load, args={"lim": limit})


async def list_notes(conn: Connection, db, args: dict) -> dict:
    """Notes logged against records."""
    limit = _limit(args)
    after = _after(args)
    params = {"limit": limit, "properties": "hs_note_body,hs_timestamp,hubspot_owner_id"}
    if after:
        params["after"] = after

    async def _load():
        body = await _get(conn, db, "crm/v3/objects/notes", params)
        out = _envelope(body, "notes")
        for n in out["notes"]:
            # The body is HTML and can run long; this is a list view.
            if n.get("hs_note_body"):
                n["hs_note_body"] = str(n["hs_note_body"])[:1000]
        return out

    return await cached(SLUG, conn.id, "list_notes", TTL_SHORT, _load,
                        args={"lim": limit, "a": after})


async def object_properties(conn: Connection, db, args: dict) -> dict:
    """Every field defined on an object type, including custom ones.

    Worth calling before searching: HubSpot portals are heavily customised and
    the property you want is often named something nobody would guess.
    """
    obj = str((args or {}).get("object_type") or "contacts").strip().lower()
    if obj not in ("contacts", "companies", "deals", "tickets"):
        raise ConnectorError(
            "object_type must be one of contacts, companies, deals, tickets."
        )

    async def _load():
        body = await _get(conn, db, f"crm/v3/properties/{obj}")
        rows = [
            {
                "name": p.get("name"),
                "label": p.get("label"),
                "type": p.get("type"),
                "field_type": p.get("fieldType"),
                "group_name": p.get("groupName"),
                "calculated": p.get("calculated"),
                "options": [o.get("value") for o in (p.get("options") or [])[:25]] or None,
            }
            for p in body.get("results", []) or []
            if not p.get("hidden")
        ]
        return {"object_type": obj, "row_count": len(rows), "properties": rows}

    return await cached(SLUG, conn.id, "object_properties", TTL_MEDIUM, _load, args={"o": obj})


# =========================================================================== #
# Catalog
# =========================================================================== #
_LIMIT = {"type": "integer", "description": "Rows to return (1-100). Default 50."}
_AFTER = {"type": "string", "description": "next_after from a previous call, to continue."}
_QUERY = {"type": "string", "description": "Free text; HubSpot matches it across the object's searchable properties."}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


_PAGING = {"limit": _LIMIT, "after": _AFTER}

CATALOG = {
    "account_info": {
        "description": "Which HubSpot portal this token belongs to, its currency and timezone.",
        "input": _obj({}),
    },
    "list_contacts": {
        "description": "Contacts with name, email, lifecycle stage and owner.",
        "input": _obj(dict(_PAGING)),
    },
    "get_contact": {
        "description": "One contact.",
        "input": _obj({"contact_id": {"type": "string"}}, ["contact_id"]),
    },
    "search_contacts": {
        "description": "Free-text search across contacts.",
        "input": _obj({"query": _QUERY, **_PAGING}, ["query"]),
    },
    "list_companies": {
        "description": "Companies with domain, industry, size and revenue.",
        "input": _obj(dict(_PAGING)),
    },
    "get_company": {
        "description": "One company.",
        "input": _obj({"company_id": {"type": "string"}}, ["company_id"]),
    },
    "search_companies": {
        "description": "Free-text search across companies.",
        "input": _obj({"query": _QUERY, **_PAGING}, ["query"]),
    },
    "list_deals": {
        "description": "Deals with amount, stage, pipeline and close date.",
        "input": _obj(dict(_PAGING)),
    },
    "get_deal": {
        "description": "One deal.",
        "input": _obj({"deal_id": {"type": "string"}}, ["deal_id"]),
    },
    "search_deals": {
        "description": "Free-text search across deals.",
        "input": _obj({"query": _QUERY, **_PAGING}, ["query"]),
    },
    "deals_by_stage": {
        "description": "Deal count and value grouped by stage. Says how many deals it counted and whether more matched.",
        "input": _obj({
            "pipeline_id": {"type": "string", "description": "Restrict to one pipeline. From list_pipelines."},
            "limit": _LIMIT,
        }),
    },
    "list_pipelines": {
        "description": "Pipelines and their stages, in display order, with win probability.",
        "input": _obj({"object_type": {"type": "string", "description": "'deals' (default) or 'tickets'."}}),
    },
    "list_tickets": {
        "description": "Support tickets with subject, pipeline stage and priority.",
        "input": _obj(dict(_PAGING)),
    },
    "list_owners": {
        "description": "Owners records can be assigned to -- use it to turn hubspot_owner_id into a name.",
        "input": _obj({"limit": _LIMIT}),
    },
    "list_notes": {
        "description": "Notes logged against records, bodies truncated for listing.",
        "input": _obj(dict(_PAGING)),
    },
    "object_properties": {
        "description": (
            "Every field defined on an object type, custom ones included. Call this "
            "first on an unfamiliar portal -- the property you want is often named "
            "something nobody would guess."
        ),
        "input": _obj({"object_type": {"type": "string", "description": "contacts (default), companies, deals or tickets."}}),
    },
}

HANDLERS = {
    "account_info": account_info,
    "list_contacts": list_contacts,
    "get_contact": get_contact,
    "search_contacts": search_contacts,
    "list_companies": list_companies,
    "get_company": get_company,
    "search_companies": search_companies,
    "list_deals": list_deals,
    "get_deal": get_deal,
    "search_deals": search_deals,
    "deals_by_stage": deals_by_stage,
    "list_pipelines": list_pipelines,
    "list_tickets": list_tickets,
    "list_owners": list_owners,
    "list_notes": list_notes,
    "object_properties": object_properties,
}

registry.register(
    Connector(
        slug=SLUG,
        label="HubSpot",
        auth="api_key",
        description=(
            "Reads a HubSpot CRM: contacts, companies, deals and tickets, the "
            "pipelines and stages behind them, owners, notes, and every property "
            "definition including custom fields."
        ),
        category="Sales",
        cred_fields=["access_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
