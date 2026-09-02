"""Open Data Hub connector — many free, keyless public data APIs in one MCP.

A single connector that bundles several well-known no-API-key data sources, each
exposed as its own tool:

  * PubMed (NCBI E-utilities)     -> pubmed_search       : biomedical literature
  * Frankfurter                    -> exchange_rate       : FX rates + conversion
  * CoinGecko                      -> crypto_price        : crypto prices & market data
  * SEC EDGAR                      -> sec_filings         : US public-company filings
  * OpenStreetMap Nominatim        -> geocode             : place -> coordinates
  * World Bank                     -> country_stat        : economic / demographic stats

None require an API key. A couple (SEC, Nominatim) require a descriptive User-Agent,
which is set below. Responses are cached.

Auth: api_key with NO cred_fields — nothing for the user to paste.
"""
from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

_UA = "TechShu-Connect-MCP/1.0 (+https://bringdata.a.techshu.in; admin@techshu.in)"
_JSON_HEADERS = {"Accept": "application/json", "User-Agent": _UA}

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
FX_BASE = "https://api.frankfurter.app"
CG_BASE = "https://api.coingecko.com/api/v3"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
WB_BASE = "https://api.worldbank.org/v2"


def _req(args: dict, *keys, label: str) -> str:
    for k in keys:
        v = args.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    raise ConnectorError(f"`{label}` is required.")


def _limit(args: dict, default: int, hi: int) -> int:
    try:
        n = int(args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


async def _get_json(url: str, params: dict | None = None, headers: dict | None = None):
    try:
        async with limit_for(url):
            res = await http_get(url, headers=headers or _JSON_HEADERS, params=params or {})
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code == 429:
        raise ConnectorError("Upstream rate limit hit. Try again shortly.")
    if res.status_code == 404:
        return None
    if res.status_code >= 400:
        raise ConnectorError(f"Upstream error {res.status_code}: {res.text[:300]}")
    try:
        return res.json()
    except ValueError:
        raise ConnectorError(f"Upstream returned non-JSON: {res.text[:300]}")


# ============================================================
# PubMed
# ============================================================

async def pubmed_search(conn: Connection, db, args: dict) -> dict:
    """Search PubMed for biomedical papers; returns title, authors, journal, date, link."""
    query = _req(args, "query", "term", "q", label="query")
    limit = _limit(args, 10, 50)

    async def _loader():
        es = await _get_json(
            f"{EUTILS}/esearch.fcgi",
            {"db": "pubmed", "term": query, "retmax": limit, "retmode": "json"},
        )
        ids = (((es or {}).get("esearchresult") or {}).get("idlist")) or []
        if not ids:
            return {"query": query, "count": 0, "results": []}
        summ = await _get_json(
            f"{EUTILS}/esummary.fcgi",
            {"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        )
        res = (summ or {}).get("result") or {}
        rows = []
        for pid in ids:
            r = res.get(pid)
            if not r:
                continue
            authors = ", ".join(a.get("name", "") for a in (r.get("authors") or [])[:6])
            rows.append({
                "pmid": pid,
                "title": r.get("title"),
                "authors": authors,
                "journal": r.get("fulljournalname") or r.get("source"),
                "pubdate": r.get("pubdate"),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            })
        return {"query": query, "count": len(rows), "results": rows}

    return await cached("open_data", conn.id, "pubmed_search", TTL_MEDIUM, _loader, args={"query": query, "limit": limit})


# ============================================================
# Currency (Frankfurter)
# ============================================================

async def exchange_rate(conn: Connection, db, args: dict) -> dict:
    """Foreign-exchange rates and conversion. Defaults base USD; pass `amount` to convert."""
    base = (args.get("from") or args.get("base") or "USD").strip().upper()
    to = args.get("to") or args.get("target")
    amount = args.get("amount")
    params: dict = {"from": base}
    if to:
        if isinstance(to, (list, tuple)):
            to = ",".join(str(t).strip().upper() for t in to)
        else:
            to = ",".join(t.strip().upper() for t in str(to).split(",") if t.strip())
        params["to"] = to
    if amount is not None:
        try:
            params["amount"] = float(amount)
        except (TypeError, ValueError):
            raise ConnectorError("`amount` must be a number.")

    async def _loader():
        data = await _get_json(f"{FX_BASE}/latest", params)
        if not data:
            raise ConnectorError("No FX data returned (check currency codes).")
        return {"base": data.get("base"), "date": data.get("date"), "amount": params.get("amount", 1), "rates": data.get("rates", {})}

    return await cached("open_data", conn.id, "exchange_rate", TTL_SHORT, _loader, args=params)


# ============================================================
# Crypto (CoinGecko)
# ============================================================

async def crypto_price(conn: Connection, db, args: dict) -> dict:
    """Crypto price + market cap + 24h change. Accepts a coin name/symbol/id and a vs currency."""
    coin = _req(args, "coin", "id", "symbol", "name", label="coin").lower()
    vs = (args.get("vs") or args.get("currency") or "usd").strip().lower()

    async def _loader():
        # Resolve a friendly name/symbol to a CoinGecko id.
        coin_id = coin
        search = await _get_json(f"{CG_BASE}/search", {"query": coin})
        coins = (search or {}).get("coins") or []
        if coins:
            exact = next((c for c in coins if c.get("symbol", "").lower() == coin or c.get("id") == coin), None)
            coin_id = (exact or coins[0]).get("id", coin)
        data = await _get_json(
            f"{CG_BASE}/simple/price",
            {"ids": coin_id, "vs_currencies": vs, "include_market_cap": "true",
             "include_24hr_change": "true", "include_last_updated_at": "true"},
        )
        info = (data or {}).get(coin_id)
        if not info:
            return {"coin": coin, "found": False, "note": "Coin not found on CoinGecko."}
        return {
            "coin": coin_id,
            "vs": vs,
            "found": True,
            "price": info.get(vs),
            "market_cap": info.get(f"{vs}_market_cap"),
            "change_24h_pct": info.get(f"{vs}_24h_change"),
        }

    return await cached("open_data", conn.id, "crypto_price", TTL_SHORT, _loader, args={"coin": coin, "vs": vs})


# ============================================================
# SEC EDGAR
# ============================================================

async def sec_filings(conn: Connection, db, args: dict) -> dict:
    """Recent SEC filings for a US public company by ticker (e.g. AAPL)."""
    ticker = _req(args, "ticker", "symbol", label="ticker").upper()
    limit = _limit(args, 15, 50)
    sec_headers = {"Accept": "application/json", "User-Agent": _UA}

    async def _loader():
        tickers = await _get_json(SEC_TICKERS, headers=sec_headers)
        cik = None
        company = None
        for row in (tickers or {}).values():
            if str(row.get("ticker", "")).upper() == ticker:
                cik = str(row.get("cik_str")).zfill(10)
                company = row.get("title")
                break
        if not cik:
            return {"ticker": ticker, "found": False, "note": "Ticker not found in SEC database."}
        sub = await _get_json(SEC_SUBMISSIONS.format(cik=cik), headers=sec_headers)
        recent = ((sub or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        rows = []
        for i in range(min(limit, len(forms))):
            acc = accns[i].replace("-", "") if i < len(accns) else ""
            rows.append({
                "form": forms[i],
                "filed": dates[i] if i < len(dates) else None,
                "accession": accns[i] if i < len(accns) else None,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{docs[i]}" if i < len(docs) and docs[i] else None,
            })
        return {"ticker": ticker, "found": True, "company": company, "cik": cik, "count": len(rows), "filings": rows}

    return await cached("open_data", conn.id, "sec_filings", TTL_MEDIUM, _loader, args={"ticker": ticker, "limit": limit})


# ============================================================
# Geocoding (OpenStreetMap Nominatim)
# ============================================================

async def geocode(conn: Connection, db, args: dict) -> dict:
    """Turn a place / address into coordinates via OpenStreetMap Nominatim."""
    query = _req(args, "query", "q", "address", "place", label="query")
    limit = _limit(args, 5, 20)

    async def _loader():
        data = await _get_json(
            NOMINATIM,
            {"q": query, "format": "json", "limit": limit, "addressdetails": 0},
            headers={"Accept": "application/json", "User-Agent": _UA},
        )
        rows = [
            {
                "name": r.get("display_name"),
                "lat": r.get("lat"),
                "lon": r.get("lon"),
                "type": r.get("type"),
                "class": r.get("class"),
                "importance": r.get("importance"),
            }
            for r in (data or [])
        ]
        return {"query": query, "count": len(rows), "results": rows}

    return await cached("open_data", conn.id, "geocode", TTL_LONG, _loader, args={"query": query, "limit": limit})


# ============================================================
# World Bank
# ============================================================

_WB_METRICS = {
    "gdp": "NY.GDP.MKTP.CD",
    "gdp_per_capita": "NY.GDP.PCAP.CD",
    "population": "SP.POP.TOTL",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
    "life_expectancy": "SP.DYN.LE00.IN",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "co2_per_capita": "EN.ATM.CO2E.PC",
}


async def country_stat(conn: Connection, db, args: dict) -> dict:
    """World Bank economic/demographic stats for a country. metric is a friendly name or raw code."""
    country = _req(args, "country", "country_code", "iso", label="country")
    metric = (args.get("metric") or "gdp").strip().lower()
    code = _WB_METRICS.get(metric, args.get("metric"))
    years = _limit(args, 5, 30)

    async def _loader():
        # Fetch a buffer of extra years so leading nulls (not-yet-published values)
        # don't starve a small `years` request, then return the latest non-null values.
        data = await _get_json(
            f"{WB_BASE}/country/{country}/indicator/{code}",
            {"format": "json", "per_page": years + 10},
        )
        # World Bank returns [meta, [rows]]
        rows_raw = data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []
        rows = [
            {"year": r.get("date"), "value": r.get("value")}
            for r in rows_raw if r.get("value") is not None
        ][:years]
        name = rows_raw[0].get("country", {}).get("value") if rows_raw else country
        return {
            "country": name,
            "metric": metric,
            "indicator_code": code,
            "available_metrics": list(_WB_METRICS.keys()),
            "series": rows,
        }

    return await cached("open_data", conn.id, "country_stat", TTL_LONG, _loader, args={"country": country, "metric": metric, "years": years})


# ============================================================
# Catalog
# ============================================================

CATALOG = {
    "pubmed_search": {
        "description": "Search PubMed for biomedical / medical research papers. Returns title, authors, journal, date and link. Source: NCBI (no key).",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms, e.g. 'metformin diabetes'."},
                "limit": {"type": "integer", "description": "Max papers (1–50)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "exchange_rate": {
        "description": "Foreign-exchange rates and currency conversion. Source: Frankfurter (ECB data, no key).",
        "input": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Base currency code (default USD)."},
                "to": {"type": "string", "description": "Target currency code(s), comma-separated. Omit for all."},
                "amount": {"type": "number", "description": "Amount to convert (default 1)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "crypto_price": {
        "description": "Crypto price, market cap and 24h change. Accepts a coin name/symbol/id (e.g. 'bitcoin' or 'btc'). Source: CoinGecko (no key).",
        "input": {
            "type": "object",
            "properties": {
                "coin": {"type": "string", "description": "Coin name, symbol or CoinGecko id."},
                "vs": {"type": "string", "description": "Quote currency (default usd)."},
            },
            "required": ["coin"],
            "additionalProperties": False,
        },
    },
    "sec_filings": {
        "description": "Recent SEC EDGAR filings for a US public company by ticker (e.g. AAPL). Source: SEC.gov (no key).",
        "input": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL, MSFT."},
                "limit": {"type": "integer", "description": "Max filings (1–50)."},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
    "geocode": {
        "description": "Turn a place name or address into latitude/longitude. Source: OpenStreetMap Nominatim (no key).",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Place or address, e.g. 'Eiffel Tower, Paris'."},
                "limit": {"type": "integer", "description": "Max matches (1–20)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "country_stat": {
        "description": "World Bank economic/demographic stats (gdp, population, inflation, unemployment, life_expectancy, ...) for a country (ISO code like US, IN, GB). Source: World Bank (no key).",
        "input": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "ISO country code, e.g. US, IN, GB."},
                "metric": {"type": "string", "description": "Friendly metric (gdp, population, inflation, unemployment, life_expectancy, gdp_growth, gdp_per_capita, co2_per_capita) or a raw World Bank code."},
                "limit": {"type": "integer", "description": "How many recent years (1–30)."},
            },
            "required": ["country"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "pubmed_search": pubmed_search,
    "exchange_rate": exchange_rate,
    "crypto_price": crypto_price,
    "sec_filings": sec_filings,
    "geocode": geocode,
    "country_stat": country_stat,
}

registry.register(
    Connector(
        slug="open_data",
        label="Open Data Hub",
        auth="api_key",
        cred_fields=[],
        catalog=CATALOG,
        handlers=HANDLERS,
        description='Reads several keyless public data APIs in one place: PubMed literature, FX rates, crypto prices, SEC EDGAR filings, OpenStreetMap geocoding and World Bank country statistics.',
        category='Reference',
    )
)
