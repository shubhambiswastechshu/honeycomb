"""Stripe connector (Stripe REST API v1, api_key).

Reads the account's money: charges and payment intents, customers and their
subscriptions, invoices, products and prices, refunds, disputes, payouts, the
balance, and rolled-up revenue.

Auth is a Stripe secret key sent as a bearer token. **Use a restricted key.**
Stripe lets you mint one scoped to read-only on exactly the resources you want,
and that is the right credential here: nothing in this file writes, every tool
is a GET, and no catalog entry carries ``write`` -- so a full-access ``sk_live_``
key would be granting far more than this connector can use. The key is stored
encrypted like any other credential, but a restricted key means a compromise
cannot move money.

Stripe paginates forward by object id rather than by page number: every list
returns ``has_more`` and the caller passes the last id back as ``starting_after``.
That is carried through faithfully instead of being hidden, because a summary
computed over one silent page is a wrong number stated confidently.

Amounts arrive as integer minor units (cents). They are returned both as Stripe
sent them and divided into a major-unit float, because ``2000`` and ``20.00``
are both the right answer to different questions and guessing which the caller
wanted is how a currency bug starts.
"""
from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

SLUG = "stripe"
BASE = "https://api.stripe.com/v1"

# Zero-decimal currencies: JPY 2000 is 2000 yen, not 20.00. Dividing by 100
# would misreport every Japanese charge by two orders of magnitude.
_ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
    "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _key(conn: Connection) -> str:
    key = str(conn.creds().get("secret_key") or "").strip()
    if not key:
        raise ConnectorError("Not connected: missing secret_key.")
    return key


def _fail(res) -> None:
    try:
        err = (res.json() or {}).get("error") or {}
        message = err.get("message") or res.text[:300]
        code = err.get("code") or err.get("type") or ""
    except ValueError:
        message, code = res.text[:300], ""
    if res.status_code == 401:
        raise ConnectorError("Stripe rejected the secret key (401).")
    if res.status_code == 403:
        raise ConnectorError(
            "Stripe refused this call (403). A restricted key needs read "
            f"permission on this resource. {str(message)[:200]}"
        )
    if res.status_code == 429:
        raise ConnectorError("Stripe rate limit hit (429). Try again shortly.")
    raise ConnectorError(f"Stripe {res.status_code}{' ' + code if code else ''}: {str(message)[:300]}")


async def _get(conn: Connection, db, path: str, params: dict | None = None) -> dict:
    url = BASE + "/" + path.lstrip("/")
    headers = {
        "Authorization": "Bearer " + _key(conn),
        "Accept": "application/json",
    }
    async with limit_for(BASE):
        try:
            res = await http_get(url, headers=headers, params=params or {})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
def _limit(args: dict, default: int = 25) -> int:
    """Stripe's own ceiling is 100."""
    try:
        return max(1, min(int((args or {}).get("limit", default)), 100))
    except (TypeError, ValueError):
        return default


def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _page(args: dict, extra: dict | None = None) -> dict:
    """limit + cursor + the common created-range filter, when supplied."""
    params = {"limit": _limit(args)}
    cursor = str((args or {}).get("starting_after") or "").strip()
    if cursor:
        params["starting_after"] = cursor
    for key, stripe_key in (("created_after", "gte"), ("created_before", "lte")):
        raw = (args or {}).get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            params[f"created[{stripe_key}]"] = int(raw)
        except (TypeError, ValueError):
            raise ConnectorError(
                f"{key} must be a Unix timestamp in seconds (e.g. 1767225600)."
            )
    for k, v in (extra or {}).items():
        if v is not None and str(v).strip() != "":
            params[k] = v
    return params


def _major(amount, currency) -> float | None:
    """Minor units to major, respecting zero-decimal currencies."""
    try:
        value = int(amount)
    except (TypeError, ValueError):
        return None
    if str(currency or "").lower() in _ZERO_DECIMAL:
        return float(value)
    return round(value / 100.0, 2)


def _envelope(body: dict, key: str, rows: list) -> dict:
    """Every list answers the same shape, cursor included."""
    data = body.get("data", []) or []
    return {
        "row_count": len(rows),
        "has_more": bool(body.get("has_more")),
        # What to pass back as starting_after to continue.
        "next_starting_after": data[-1].get("id") if data and body.get("has_more") else "",
        key: rows,
    }


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #
def _charge(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "created": c.get("created"),
        "amount": c.get("amount"),
        "amount_major": _major(c.get("amount"), c.get("currency")),
        "amount_refunded": c.get("amount_refunded"),
        "currency": c.get("currency"),
        "status": c.get("status"),
        "paid": c.get("paid"),
        "refunded": c.get("refunded"),
        "disputed": c.get("disputed"),
        "description": c.get("description"),
        "customer": c.get("customer"),
        "payment_intent": c.get("payment_intent"),
        "receipt_email": c.get("receipt_email"),
        "failure_code": c.get("failure_code"),
        "failure_message": c.get("failure_message"),
        "card_brand": ((c.get("payment_method_details") or {}).get("card") or {}).get("brand"),
        "card_last4": ((c.get("payment_method_details") or {}).get("card") or {}).get("last4"),
    }


def _customer_row(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "created": c.get("created"),
        "email": c.get("email"),
        "name": c.get("name"),
        "description": c.get("description"),
        "currency": c.get("currency"),
        "delinquent": c.get("delinquent"),
        "balance": c.get("balance"),
        "livemode": c.get("livemode"),
    }


def _subscription(s: dict) -> dict:
    items = [
        {
            "price_id": (i.get("price") or {}).get("id"),
            "product": (i.get("price") or {}).get("product"),
            "unit_amount": (i.get("price") or {}).get("unit_amount"),
            "interval": ((i.get("price") or {}).get("recurring") or {}).get("interval"),
            "quantity": i.get("quantity"),
        }
        for i in ((s.get("items") or {}).get("data") or [])
    ]
    return {
        "id": s.get("id"),
        "customer": s.get("customer"),
        "status": s.get("status"),
        "created": s.get("created"),
        "current_period_start": s.get("current_period_start"),
        "current_period_end": s.get("current_period_end"),
        "cancel_at_period_end": s.get("cancel_at_period_end"),
        "canceled_at": s.get("canceled_at"),
        "trial_end": s.get("trial_end"),
        "currency": s.get("currency"),
        "items": items,
    }


def _invoice(i: dict) -> dict:
    return {
        "id": i.get("id"),
        "number": i.get("number"),
        "created": i.get("created"),
        "status": i.get("status"),
        "customer": i.get("customer"),
        "customer_email": i.get("customer_email"),
        "currency": i.get("currency"),
        "total": i.get("total"),
        "total_major": _major(i.get("total"), i.get("currency")),
        "amount_paid": i.get("amount_paid"),
        "amount_due": i.get("amount_due"),
        "due_date": i.get("due_date"),
        "paid": i.get("paid"),
        "hosted_invoice_url": i.get("hosted_invoice_url"),
        "subscription": i.get("subscription"),
    }


# =========================================================================== #
# Account and balance
# =========================================================================== #
async def account(conn: Connection, db, args: dict) -> dict:
    """Which Stripe account this key belongs to, and whether it is live."""

    async def _load():
        a = await _get(conn, db, "account")
        return {
            "id": a.get("id"),
            "business_name": (a.get("business_profile") or {}).get("name"),
            "country": a.get("country"),
            "default_currency": a.get("default_currency"),
            "charges_enabled": a.get("charges_enabled"),
            "payouts_enabled": a.get("payouts_enabled"),
            "details_submitted": a.get("details_submitted"),
            "type": a.get("type"),
        }

    return await cached(SLUG, conn.id, "account", TTL_MEDIUM, _load)


async def balance(conn: Connection, db, args: dict) -> dict:
    """What is available now and what is still settling."""

    async def _load():
        b = await _get(conn, db, "balance")

        def pot(rows):
            return [
                {"currency": r.get("currency"), "amount": r.get("amount"),
                 "amount_major": _major(r.get("amount"), r.get("currency"))}
                for r in rows or []
            ]

        return {
            "livemode": b.get("livemode"),
            "available": pot(b.get("available")),
            "pending": pot(b.get("pending")),
            "connect_reserved": pot(b.get("connect_reserved")),
        }

    return await cached(SLUG, conn.id, "balance", TTL_SHORT, _load)


# =========================================================================== #
# Payments
# =========================================================================== #
async def list_charges(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"customer": (args or {}).get("customer_id")})

    async def _load():
        body = await _get(conn, db, "charges", params)
        return _envelope(body, "charges", [_charge(c) for c in body.get("data", []) or []])

    return await cached(SLUG, conn.id, "list_charges", TTL_SHORT, _load, args=params)


async def get_charge(conn: Connection, db, args: dict) -> dict:
    cid = _require(args, "charge_id", "ch_3Ab...")

    async def _load():
        return _charge(await _get(conn, db, f"charges/{cid}"))

    return await cached(SLUG, conn.id, "get_charge", TTL_SHORT, _load, args={"c": cid})


async def list_payment_intents(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"customer": (args or {}).get("customer_id")})

    async def _load():
        body = await _get(conn, db, "payment_intents", params)
        rows = [
            {
                "id": p.get("id"),
                "created": p.get("created"),
                "status": p.get("status"),
                "amount": p.get("amount"),
                "amount_major": _major(p.get("amount"), p.get("currency")),
                "amount_received": p.get("amount_received"),
                "currency": p.get("currency"),
                "customer": p.get("customer"),
                "description": p.get("description"),
                "cancellation_reason": p.get("cancellation_reason"),
                "last_payment_error": (p.get("last_payment_error") or {}).get("message"),
            }
            for p in body.get("data", []) or []
        ]
        return _envelope(body, "payment_intents", rows)

    return await cached(SLUG, conn.id, "list_payment_intents", TTL_SHORT, _load, args=params)


async def get_payment_intent(conn: Connection, db, args: dict) -> dict:
    pid = _require(args, "payment_intent_id", "pi_3Ab...")

    async def _load():
        p = await _get(conn, db, f"payment_intents/{pid}")
        return {
            "id": p.get("id"), "status": p.get("status"), "created": p.get("created"),
            "amount": p.get("amount"),
            "amount_major": _major(p.get("amount"), p.get("currency")),
            "currency": p.get("currency"), "customer": p.get("customer"),
            "description": p.get("description"),
            "payment_method_types": p.get("payment_method_types"),
            "last_payment_error": (p.get("last_payment_error") or {}).get("message"),
        }

    return await cached(SLUG, conn.id, "get_payment_intent", TTL_SHORT, _load, args={"p": pid})


async def list_refunds(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"charge": (args or {}).get("charge_id")})

    async def _load():
        body = await _get(conn, db, "refunds", params)
        rows = [
            {
                "id": r.get("id"), "created": r.get("created"),
                "amount": r.get("amount"),
                "amount_major": _major(r.get("amount"), r.get("currency")),
                "currency": r.get("currency"), "status": r.get("status"),
                "reason": r.get("reason"), "charge": r.get("charge"),
                "payment_intent": r.get("payment_intent"),
            }
            for r in body.get("data", []) or []
        ]
        return _envelope(body, "refunds", rows)

    return await cached(SLUG, conn.id, "list_refunds", TTL_SHORT, _load, args=params)


async def list_disputes(conn: Connection, db, args: dict) -> dict:
    params = _page(args)

    async def _load():
        body = await _get(conn, db, "disputes", params)
        rows = [
            {
                "id": d.get("id"), "created": d.get("created"),
                "amount": d.get("amount"),
                "amount_major": _major(d.get("amount"), d.get("currency")),
                "currency": d.get("currency"), "status": d.get("status"),
                "reason": d.get("reason"), "charge": d.get("charge"),
                "evidence_due_by": (d.get("evidence_details") or {}).get("due_by"),
                "is_charge_refundable": d.get("is_charge_refundable"),
            }
            for d in body.get("data", []) or []
        ]
        return _envelope(body, "disputes", rows)

    return await cached(SLUG, conn.id, "list_disputes", TTL_SHORT, _load, args=params)


async def list_payouts(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"status": (args or {}).get("status")})

    async def _load():
        body = await _get(conn, db, "payouts", params)
        rows = [
            {
                "id": p.get("id"), "created": p.get("created"),
                "arrival_date": p.get("arrival_date"),
                "amount": p.get("amount"),
                "amount_major": _major(p.get("amount"), p.get("currency")),
                "currency": p.get("currency"), "status": p.get("status"),
                "method": p.get("method"), "type": p.get("type"),
                "failure_message": p.get("failure_message"),
            }
            for p in body.get("data", []) or []
        ]
        return _envelope(body, "payouts", rows)

    return await cached(SLUG, conn.id, "list_payouts", TTL_SHORT, _load, args=params)


# =========================================================================== #
# Customers and recurring revenue
# =========================================================================== #
async def list_customers(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"email": (args or {}).get("email")})

    async def _load():
        body = await _get(conn, db, "customers", params)
        rows = [_customer_row(c) for c in body.get("data", []) or []]
        return _envelope(body, "customers", rows)

    return await cached(SLUG, conn.id, "list_customers", TTL_SHORT, _load, args=params)


async def get_customer(conn: Connection, db, args: dict) -> dict:
    cid = _require(args, "customer_id", "cus_Ab...")

    async def _load():
        return _customer_row(await _get(conn, db, f"customers/{cid}"))

    return await cached(SLUG, conn.id, "get_customer", TTL_SHORT, _load, args={"c": cid})


async def search_customers(conn: Connection, db, args: dict) -> dict:
    """Stripe's own search query language, e.g. ``email:'a@b.com'``."""
    query = _require(args, "query", "email:'someone@example.com'")
    limit = _limit(args)

    async def _load():
        body = await _get(conn, db, "customers/search", {"query": query, "limit": limit})
        rows = [_customer_row(c) for c in body.get("data", []) or []]
        return {
            "query": query, "row_count": len(rows),
            "has_more": bool(body.get("has_more")),
            "next_page": body.get("next_page") or "",
            "customers": rows,
        }

    return await cached(SLUG, conn.id, "search_customers", TTL_SHORT, _load,
                        args={"q": query, "lim": limit})


async def list_subscriptions(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {
        "customer": (args or {}).get("customer_id"),
        "status": (args or {}).get("status"),
        "price": (args or {}).get("price_id"),
    })

    async def _load():
        body = await _get(conn, db, "subscriptions", params)
        rows = [_subscription(s) for s in body.get("data", []) or []]
        return _envelope(body, "subscriptions", rows)

    return await cached(SLUG, conn.id, "list_subscriptions", TTL_SHORT, _load, args=params)


async def get_subscription(conn: Connection, db, args: dict) -> dict:
    sid = _require(args, "subscription_id", "sub_Ab...")

    async def _load():
        return _subscription(await _get(conn, db, f"subscriptions/{sid}"))

    return await cached(SLUG, conn.id, "get_subscription", TTL_SHORT, _load, args={"s": sid})


async def list_invoices(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {
        "customer": (args or {}).get("customer_id"),
        "status": (args or {}).get("status"),
        "subscription": (args or {}).get("subscription_id"),
    })

    async def _load():
        body = await _get(conn, db, "invoices", params)
        rows = [_invoice(i) for i in body.get("data", []) or []]
        return _envelope(body, "invoices", rows)

    return await cached(SLUG, conn.id, "list_invoices", TTL_SHORT, _load, args=params)


async def get_invoice(conn: Connection, db, args: dict) -> dict:
    iid = _require(args, "invoice_id", "in_Ab...")

    async def _load():
        i = await _get(conn, db, f"invoices/{iid}")
        out = _invoice(i)
        out["lines"] = [
            {
                "description": l.get("description"),
                "amount": l.get("amount"),
                "quantity": l.get("quantity"),
                "price_id": (l.get("price") or {}).get("id"),
            }
            for l in ((i.get("lines") or {}).get("data") or [])[:100]
        ]
        return out

    return await cached(SLUG, conn.id, "get_invoice", TTL_SHORT, _load, args={"i": iid})


# =========================================================================== #
# Catalog
# =========================================================================== #
async def list_products(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {"active": (args or {}).get("active")})

    async def _load():
        body = await _get(conn, db, "products", params)
        rows = [
            {
                "id": p.get("id"), "name": p.get("name"), "active": p.get("active"),
                "description": p.get("description"), "created": p.get("created"),
                "default_price": p.get("default_price"),
            }
            for p in body.get("data", []) or []
        ]
        return _envelope(body, "products", rows)

    return await cached(SLUG, conn.id, "list_products", TTL_MEDIUM, _load, args=params)


async def list_prices(conn: Connection, db, args: dict) -> dict:
    params = _page(args, {
        "product": (args or {}).get("product_id"),
        "active": (args or {}).get("active"),
    })

    async def _load():
        body = await _get(conn, db, "prices", params)
        rows = [
            {
                "id": p.get("id"), "product": p.get("product"), "active": p.get("active"),
                "currency": p.get("currency"),
                "unit_amount": p.get("unit_amount"),
                "unit_amount_major": _major(p.get("unit_amount"), p.get("currency")),
                "type": p.get("type"),
                "interval": (p.get("recurring") or {}).get("interval"),
                "interval_count": (p.get("recurring") or {}).get("interval_count"),
                "nickname": p.get("nickname"),
            }
            for p in body.get("data", []) or []
        ]
        return _envelope(body, "prices", rows)

    return await cached(SLUG, conn.id, "list_prices", TTL_MEDIUM, _load, args=params)


# =========================================================================== #
# Rollups
# =========================================================================== #
async def revenue_summary(conn: Connection, db, args: dict) -> dict:
    """Succeeded charges, refunds and net, summed per currency over one page.

    Grouped by currency rather than added together: summing USD and EUR into
    one number would be arithmetic on incomparable units.
    """
    params = _page(args)
    params["limit"] = 100

    async def _load():
        body = await _get(conn, db, "charges", params)
        rows = body.get("data", []) or []
        per: dict = {}
        for c in rows:
            cur = (c.get("currency") or "").lower()
            slot = per.setdefault(cur, {
                "currency": cur, "charges": 0, "succeeded": 0, "failed": 0,
                "gross": 0, "refunded": 0,
            })
            slot["charges"] += 1
            if c.get("status") == "succeeded":
                slot["succeeded"] += 1
                slot["gross"] += int(c.get("amount") or 0)
                slot["refunded"] += int(c.get("amount_refunded") or 0)
            elif c.get("status") == "failed":
                slot["failed"] += 1
        out = []
        for cur, s in sorted(per.items()):
            s["gross_major"] = _major(s["gross"], cur)
            s["refunded_major"] = _major(s["refunded"], cur)
            s["net"] = s["gross"] - s["refunded"]
            s["net_major"] = _major(s["net"], cur)
            out.append(s)
        return {
            "charges_counted": len(rows),
            "complete": not body.get("has_more"),
            "note": "" if not body.get("has_more") else
                    "More charges match than were counted -- this covers the first 100 only.",
            "by_currency": out,
        }

    return await cached(SLUG, conn.id, "revenue_summary", TTL_SHORT, _load, args=params)


async def list_events(conn: Connection, db, args: dict) -> dict:
    """Recent account events -- Stripe's own audit trail of what changed."""
    params = _page(args, {"type": (args or {}).get("type")})

    async def _load():
        body = await _get(conn, db, "events", params)
        rows = [
            {
                "id": e.get("id"), "created": e.get("created"), "type": e.get("type"),
                "livemode": e.get("livemode"),
                "object_id": ((e.get("data") or {}).get("object") or {}).get("id"),
            }
            for e in body.get("data", []) or []
        ]
        return _envelope(body, "events", rows)

    return await cached(SLUG, conn.id, "list_events", TTL_SHORT, _load, args=params)


# =========================================================================== #
# Catalog declaration
# =========================================================================== #
_LIMIT = {"type": "integer", "description": "Rows to return (1-100). Default 25."}
_AFTER = {"type": "string", "description": "next_starting_after from a previous call, to continue."}
_CFROM = {"type": "integer", "description": "Unix timestamp in seconds; only objects created at or after it."}
_CTO = {"type": "integer", "description": "Unix timestamp in seconds; only objects created at or before it."}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


_PAGING = {"limit": _LIMIT, "starting_after": _AFTER,
           "created_after": _CFROM, "created_before": _CTO}

CATALOG = {
    "account": {
        "description": "Which Stripe account this key belongs to, its country, currency and whether charges and payouts are enabled.",
        "input": _obj({}),
    },
    "balance": {
        "description": "Funds available now and still pending, per currency.",
        "input": _obj({}),
    },
    "list_charges": {
        "description": "Charges, newest first, with card brand and failure reason where there is one.",
        "input": _obj({**_PAGING, "customer_id": {"type": "string"}}),
    },
    "get_charge": {
        "description": "One charge in full.",
        "input": _obj({"charge_id": {"type": "string"}}, ["charge_id"]),
    },
    "list_payment_intents": {
        "description": "Payment intents with status and any last payment error.",
        "input": _obj({**_PAGING, "customer_id": {"type": "string"}}),
    },
    "get_payment_intent": {
        "description": "One payment intent in full.",
        "input": _obj({"payment_intent_id": {"type": "string"}}, ["payment_intent_id"]),
    },
    "list_refunds": {
        "description": "Refunds, optionally for one charge.",
        "input": _obj({**_PAGING, "charge_id": {"type": "string"}}),
    },
    "list_disputes": {
        "description": "Chargebacks and disputes, with evidence deadlines.",
        "input": _obj(dict(_PAGING)),
    },
    "list_payouts": {
        "description": "Transfers to the bank account, with arrival dates and failures.",
        "input": _obj({**_PAGING, "status": {"type": "string", "description": "paid, pending, in_transit, canceled or failed."}}),
    },
    "list_customers": {
        "description": "Customers, newest first.",
        "input": _obj({**_PAGING, "email": {"type": "string", "description": "Exact email match."}}),
    },
    "get_customer": {
        "description": "One customer.",
        "input": _obj({"customer_id": {"type": "string"}}, ["customer_id"]),
    },
    "search_customers": {
        "description": "Stripe's customer search, using its query language, e.g. \"email:'a@b.com'\" or \"metadata['plan']:'pro'\".",
        "input": _obj({"query": {"type": "string"}, "limit": _LIMIT}, ["query"]),
    },
    "list_subscriptions": {
        "description": "Subscriptions with their items, prices and period boundaries.",
        "input": _obj({
            **_PAGING,
            "customer_id": {"type": "string"},
            "status": {"type": "string", "description": "active, past_due, canceled, trialing, all, ..."},
            "price_id": {"type": "string"},
        }),
    },
    "get_subscription": {
        "description": "One subscription in full.",
        "input": _obj({"subscription_id": {"type": "string"}}, ["subscription_id"]),
    },
    "list_invoices": {
        "description": "Invoices with totals, amounts paid and due, and their hosted URLs.",
        "input": _obj({
            **_PAGING,
            "customer_id": {"type": "string"},
            "subscription_id": {"type": "string"},
            "status": {"type": "string", "description": "draft, open, paid, uncollectible or void."},
        }),
    },
    "get_invoice": {
        "description": "One invoice, including its line items.",
        "input": _obj({"invoice_id": {"type": "string"}}, ["invoice_id"]),
    },
    "list_products": {
        "description": "Products in the Stripe catalog.",
        "input": _obj({**_PAGING, "active": {"type": "boolean"}}),
    },
    "list_prices": {
        "description": "Prices, with recurring interval where they are subscriptions.",
        "input": _obj({**_PAGING, "product_id": {"type": "string"}, "active": {"type": "boolean"}}),
    },
    "revenue_summary": {
        "description": "Gross, refunded and net from succeeded charges, grouped by currency. Says how many charges it counted and whether more matched.",
        "input": _obj({"created_after": _CFROM, "created_before": _CTO}),
    },
    "list_events": {
        "description": "Recent account events -- Stripe's own record of what changed.",
        "input": _obj({**_PAGING, "type": {"type": "string", "description": "Event type filter, e.g. 'charge.succeeded'."}}),
    },
}

HANDLERS = {
    "account": account,
    "balance": balance,
    "list_charges": list_charges,
    "get_charge": get_charge,
    "list_payment_intents": list_payment_intents,
    "get_payment_intent": get_payment_intent,
    "list_refunds": list_refunds,
    "list_disputes": list_disputes,
    "list_payouts": list_payouts,
    "list_customers": list_customers,
    "get_customer": get_customer,
    "search_customers": search_customers,
    "list_subscriptions": list_subscriptions,
    "get_subscription": get_subscription,
    "list_invoices": list_invoices,
    "get_invoice": get_invoice,
    "list_products": list_products,
    "list_prices": list_prices,
    "revenue_summary": revenue_summary,
    "list_events": list_events,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Stripe",
        auth="api_key",
        description=(
            "Reads a Stripe account: charges, payment intents, customers, "
            "subscriptions, invoices, products and prices, refunds, disputes, "
            "payouts, balance and revenue rollups. Read-only -- use a restricted key."
        ),
        category="Commerce",
        cred_fields=["secret_key"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
