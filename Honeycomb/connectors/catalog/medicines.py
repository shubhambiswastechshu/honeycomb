"""Medicines connector — drug data from openFDA + RxNorm/RxNav (NO API key).

Two free, keyless US government data sources power this connector:

  * openFDA (https://api.fda.gov) — FDA drug labels, adverse-event reports (FAERS),
    recalls/enforcement, and the National Drug Code (NDC) directory.
  * RxNav / RxNorm (https://rxnav.nlm.nih.gov) — the NIH NLM drug-name normaliser:
    resolve a name to an RxCUI, list brand/generic variants, and fix misspellings.

Neither requires an API key. openFDA throttles anonymous use to ~240 req/min and
1000 req/day per IP, which is plenty for interactive use; responses are cached.

Auth: api_key with NO cred_fields — nothing for the user to paste.

Tools:
  drug_label          : FDA label — indications, dosage, warnings, side effects
  adverse_events      : top reported adverse reactions for a drug (FAERS)
  drug_recalls        : FDA recall / enforcement reports for a drug
  ndc_lookup          : National Drug Code directory entry (packaging, manufacturer)
  rxnorm_lookup       : normalise a drug name to its RxCUI + standard name
  drug_variants       : brand & generic variants of a drug (RxNav)
  spelling_suggestions: approximate-match suggestions for a misspelled drug name
"""
from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

FDA_BASE = "https://api.fda.gov"
RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"

_HEADERS = {"Accept": "application/json", "User-Agent": "TechShu-Connect-MCP/1.0 (+medicines connector)"}


def _name(args: dict) -> str:
    n = (args.get("name") or args.get("drug") or args.get("query") or "").strip()
    if not n:
        raise ConnectorError("`name` is required (a drug brand or generic name).")
    return n


def _limit(args: dict, default: int, hi: int) -> int:
    try:
        n = int(args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


async def _get_json(url: str, params: dict) -> dict:
    try:
        async with limit_for(url):
            res = await http_get(url, headers=_HEADERS, params=params)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    # openFDA returns 404 with {"error": {...}} when nothing matches — treat as empty.
    if res.status_code == 404:
        return {"results": []}
    if res.status_code == 429:
        raise ConnectorError("Rate limit hit on the upstream (openFDA/RxNav). Try again shortly.")
    if res.status_code >= 400:
        raise ConnectorError(f"Upstream error {res.status_code}: {res.text[:300]}")
    try:
        return res.json()
    except ValueError:
        raise ConnectorError(f"Upstream returned non-JSON: {res.text[:300]}")


def _or_search(field_a: str, field_b: str, value: str) -> str:
    v = value.replace('"', "")
    return f'{field_a}:"{v}" OR {field_b}:"{v}"'


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str) and v:
            return v
    return None


# ============================================================
# openFDA tools
# ============================================================

async def drug_label(conn: Connection, db, args: dict) -> dict:
    """FDA-approved label: indications, dosage, warnings, adverse reactions."""
    name = _name(args)

    async def _loader():
        data = await _get_json(
            f"{FDA_BASE}/drug/label.json",
            {"search": _or_search("openfda.brand_name", "openfda.generic_name", name), "limit": 1},
        )
        results = data.get("results") or []
        if not results:
            return {"name": name, "found": False, "note": "No FDA label found for that name."}
        r = results[0]
        of = r.get("openfda", {})
        return {
            "name": name,
            "found": True,
            "brand_names": of.get("brand_name", []),
            "generic_names": of.get("generic_name", []),
            "manufacturer": of.get("manufacturer_name", []),
            "route": of.get("route", []),
            "indications": _first(r, "indications_and_usage"),
            "dosage": _first(r, "dosage_and_administration"),
            "warnings": _first(r, "warnings", "warnings_and_cautions", "boxed_warning"),
            "adverse_reactions": _first(r, "adverse_reactions"),
            "contraindications": _first(r, "contraindications"),
            "drug_interactions": _first(r, "drug_interactions"),
            "pregnancy": _first(r, "pregnancy"),
            "how_supplied": _first(r, "how_supplied"),
        }

    return await cached("medicines", conn.id, "drug_label", TTL_LONG, _loader, args={"name": name})


async def adverse_events(conn: Connection, db, args: dict) -> dict:
    """Top reported adverse reactions for a drug from the FDA FAERS database."""
    name = _name(args)
    limit = _limit(args, 15, 50)

    async def _loader():
        search = _or_search(
            "patient.drug.openfda.brand_name", "patient.drug.openfda.generic_name", name
        )
        data = await _get_json(
            f"{FDA_BASE}/drug/event.json",
            {"search": search, "count": "patient.reaction.reactionmeddrapt.exact", "limit": limit},
        )
        rows = data.get("results") or []
        if not rows:
            return {"name": name, "found": False, "note": "No adverse-event reports found."}
        return {
            "name": name,
            "found": True,
            "top_reactions": [{"reaction": r.get("term"), "reports": r.get("count")} for r in rows],
        }

    return await cached("medicines", conn.id, "adverse_events", TTL_MEDIUM, _loader, args={"name": name, "limit": limit})


async def drug_recalls(conn: Connection, db, args: dict) -> dict:
    """FDA recall / enforcement reports for a drug."""
    name = _name(args)
    limit = _limit(args, 10, 50)

    async def _loader():
        data = await _get_json(
            f"{FDA_BASE}/drug/enforcement.json",
            {"search": f'product_description:"{name.replace(chr(34), "")}"', "limit": limit},
        )
        rows = data.get("results") or []
        return {
            "name": name,
            "count": len(rows),
            "recalls": [
                {
                    "status": r.get("status"),
                    "classification": r.get("classification"),
                    "reason": r.get("reason_for_recall"),
                    "firm": r.get("recalling_firm"),
                    "product": r.get("product_description"),
                    "distribution": r.get("distribution_pattern"),
                    "recall_date": r.get("recall_initiation_date"),
                }
                for r in rows
            ],
        }

    return await cached("medicines", conn.id, "drug_recalls", TTL_MEDIUM, _loader, args={"name": name, "limit": limit})


async def ndc_lookup(conn: Connection, db, args: dict) -> dict:
    """National Drug Code (NDC) directory: packaging, dosage form, route, labeler."""
    name = _name(args)
    limit = _limit(args, 10, 50)

    async def _loader():
        data = await _get_json(
            f"{FDA_BASE}/drug/ndc.json",
            {"search": _or_search("brand_name", "generic_name", name), "limit": limit},
        )
        rows = data.get("results") or []
        return {
            "name": name,
            "count": len(rows),
            "products": [
                {
                    "product_ndc": r.get("product_ndc"),
                    "brand_name": r.get("brand_name"),
                    "generic_name": r.get("generic_name"),
                    "dosage_form": r.get("dosage_form"),
                    "route": r.get("route"),
                    "labeler": r.get("labeler_name"),
                    "active_ingredients": r.get("active_ingredients"),
                    "marketing_category": r.get("marketing_category"),
                }
                for r in rows
            ],
        }

    return await cached("medicines", conn.id, "ndc_lookup", TTL_LONG, _loader, args={"name": name, "limit": limit})


# ============================================================
# RxNorm / RxNav tools
# ============================================================

async def rxnorm_lookup(conn: Connection, db, args: dict) -> dict:
    """Normalise a drug name to its RxCUI (RxNorm concept id) and standard name."""
    name = _name(args)

    async def _loader():
        data = await _get_json(f"{RXNAV_BASE}/rxcui.json", {"name": name, "search": 2})
        ids = (data.get("idGroup") or {}).get("rxnormId") or []
        out = {"name": name, "rxcui": ids[0] if ids else None, "all_rxcui": ids}
        if ids:
            props = await _get_json(f"{RXNAV_BASE}/rxcui/{ids[0]}/properties.json", {})
            p = props.get("properties") or {}
            out["standard_name"] = p.get("name")
            out["term_type"] = p.get("tty")
        return out

    return await cached("medicines", conn.id, "rxnorm_lookup", TTL_LONG, _loader, args={"name": name})


async def drug_variants(conn: Connection, db, args: dict) -> dict:
    """Brand and generic variants / related drug products for a name (RxNav)."""
    name = _name(args)

    async def _loader():
        data = await _get_json(f"{RXNAV_BASE}/drugs.json", {"name": name})
        groups = (data.get("drugGroup") or {}).get("conceptGroup") or []
        variants: list[dict] = []
        for g in groups:
            tty = g.get("tty")
            for c in g.get("conceptProperties", []) or []:
                variants.append({"name": c.get("name"), "rxcui": c.get("rxcui"), "type": tty})
        return {"name": name, "count": len(variants), "variants": variants}

    return await cached("medicines", conn.id, "drug_variants", TTL_LONG, _loader, args={"name": name})


async def spelling_suggestions(conn: Connection, db, args: dict) -> dict:
    """Approximate-match suggestions for a (possibly misspelled) drug name."""
    term = (args.get("term") or args.get("name") or args.get("query") or "").strip()
    if not term:
        raise ConnectorError("`term` is required.")
    limit = _limit(args, 10, 30)

    async def _loader():
        data = await _get_json(f"{RXNAV_BASE}/approximateTerm.json", {"term": term, "maxEntries": limit})
        cands = (data.get("approximateGroup") or {}).get("candidate") or []
        seen, out = set(), []
        for c in cands:
            nm = c.get("name")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            out.append({"name": nm, "rxcui": c.get("rxcui"), "score": c.get("score")})
        return {"term": term, "count": len(out), "suggestions": out}

    return await cached("medicines", conn.id, "spelling_suggestions", TTL_MEDIUM, _loader, args={"term": term, "limit": limit})


# ============================================================
# Catalog
# ============================================================

_NAME_PROP = {"name": {"type": "string", "description": "Drug brand or generic name, e.g. 'ibuprofen' or 'Advil'."}}
_NAME_LIMIT = {
    **_NAME_PROP,
    "limit": {"type": "integer", "description": "Max rows to return."},
}

CATALOG = {
    "drug_label": {
        "description": "FDA-approved drug label: indications, dosage, warnings, contraindications, interactions and adverse reactions. Source: openFDA (no key).",
        "input": {"type": "object", "properties": dict(_NAME_PROP), "required": ["name"], "additionalProperties": False},
    },
    "adverse_events": {
        "description": "Top reported adverse reactions for a drug from the FDA FAERS adverse-event database, ranked by report count. Source: openFDA.",
        "input": {"type": "object", "properties": dict(_NAME_LIMIT), "required": ["name"], "additionalProperties": False},
    },
    "drug_recalls": {
        "description": "FDA recall / enforcement reports for a drug (reason, classification, recalling firm, status). Source: openFDA.",
        "input": {"type": "object", "properties": dict(_NAME_LIMIT), "required": ["name"], "additionalProperties": False},
    },
    "ndc_lookup": {
        "description": "National Drug Code (NDC) directory entry: packaging, dosage form, route, labeler and active ingredients. Source: openFDA.",
        "input": {"type": "object", "properties": dict(_NAME_LIMIT), "required": ["name"], "additionalProperties": False},
    },
    "rxnorm_lookup": {
        "description": "Normalise a drug name to its RxCUI (RxNorm concept id) and standard name. Source: NIH RxNav (no key).",
        "input": {"type": "object", "properties": dict(_NAME_PROP), "required": ["name"], "additionalProperties": False},
    },
    "drug_variants": {
        "description": "List brand & generic variants and related drug products for a name. Source: NIH RxNav.",
        "input": {"type": "object", "properties": dict(_NAME_PROP), "required": ["name"], "additionalProperties": False},
    },
    "spelling_suggestions": {
        "description": "Approximate-match suggestions for a misspelled drug name. Source: NIH RxNav.",
        "input": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "The (possibly misspelled) drug name."},
                "limit": {"type": "integer", "description": "Max suggestions (1–30)."},
            },
            "required": ["term"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "drug_label": drug_label,
    "adverse_events": adverse_events,
    "drug_recalls": drug_recalls,
    "ndc_lookup": ndc_lookup,
    "rxnorm_lookup": rxnorm_lookup,
    "drug_variants": drug_variants,
    "spelling_suggestions": spelling_suggestions,
}

registry.register(
    Connector(
        slug="medicines",
        label="Medicines & Drug Data",
        auth="api_key",
        cred_fields=[],
        catalog=CATALOG,
        handlers=HANDLERS,
        description='Reads FDA drug labels, adverse-event reports, recalls and NDC entries from openFDA, plus RxNorm/RxNav drug-name normalisation. No API key.',
        category='Reference',
    )
)
