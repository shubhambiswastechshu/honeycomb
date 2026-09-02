"""Google Keyword Planner connector (Google Ads API KeywordPlanIdeaService).

Exposes Keyword Planner ideas, historical search-volume lookups, and a set of
derived/heuristic research tools via the Google Ads REST API
(`GenerateKeywordIdeas` and `GenerateKeywordHistoricalMetrics`).

Auth is google_oauth (scope: adwords) and additionally requires a server-level
developer token (settings.GOOGLE_ADS_DEVELOPER_TOKEN). The connecting user saves a
{"customer_id": "..."} credential (the account whose Keyword Planner is queried);
an optional login_customer_id (manager account) header may also be supplied. When
no customer_id is provided we discover the first accessible customer via
listAccessibleCustomers.

Core endpoints:
    POST {base}/{v}/customers/{cid}:generateKeywordIdeas
    POST {base}/{v}/customers/{cid}:generateKeywordHistoricalMetrics
    GET  {base}/{v}/customers:listAccessibleCustomers
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post
from connections.models import Connection


# --------------------------------------------------------------------------- #
# Geo / language constants
# Common ISO country code -> Google Ads geoTargetConstant ID.
# Full list: https://developers.google.com/google-ads/api/reference/data/geotargets
# --------------------------------------------------------------------------- #
GEO_MAP = {
    "US": 2840, "UK": 2826, "GB": 2826, "CA": 2124, "AU": 2036,
    "IN": 2356, "DE": 2276, "FR": 2250, "ES": 2724, "IT": 2380,
    "JP": 2392, "BR": 2076, "MX": 2484, "ZA": 2710, "AE": 2784,
    "SG": 2702, "NL": 2528, "SE": 2752, "CH": 2756, "IE": 2372,
    "NZ": 2554, "PH": 2608, "PK": 2586, "BD": 2050, "ID": 2360,
}

# ISO language -> Google Ads languageConstant ID.
# Full list: https://developers.google.com/google-ads/api/reference/data/codes-formats#languages
LANG_MAP = {
    "en": 1000, "de": 1001, "es": 1003, "fr": 1002, "it": 1004,
    "pt": 1014, "nl": 1010, "ja": 1005, "ko": 1012, "zh": 1017,
    "ru": 1031, "ar": 1019, "hi": 1023, "bn": 1098, "ur": 1041,
    "tr": 1037, "id": 1025, "vi": 1040, "th": 1044, "pl": 1030,
}

# Google's competition enum (kept as-is in output, mirroring RAVEN)
_MONTH_ENUM = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
               "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]

_MONTH_INDEX = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


def _geo_constants(country) -> list[str]:
    """Accept 'US', 'us', ['US', 'IN'], digits, None -> list of full geoTargetConstants paths."""
    if not country:
        return [f"geoTargetConstants/{GEO_MAP['US']}"]
    if isinstance(country, str):
        country = [country]
    out: list[str] = []
    for c in country:
        if not c:
            continue
        if str(c).isdigit():
            out.append(f"geoTargetConstants/{c}")
            continue
        gid = GEO_MAP.get(str(c).upper())
        if gid:
            out.append(f"geoTargetConstants/{gid}")
    return out or [f"geoTargetConstants/{GEO_MAP['US']}"]


def _lang_constant(lang) -> str:
    if not lang:
        return f"languageConstants/{LANG_MAP['en']}"
    if str(lang).isdigit():
        return f"languageConstants/{lang}"
    lid = LANG_MAP.get(str(lang).lower(), LANG_MAP["en"])
    return f"languageConstants/{lid}"


# --------------------------------------------------------------------------- #
# Config / token plumbing
# --------------------------------------------------------------------------- #
def _base() -> str:
    return getattr(settings, "GOOGLE_ADS_BASE_URL", "https://googleads.googleapis.com").rstrip("/")


def _version() -> str:
    return getattr(settings, "GOOGLE_ADS_API_VERSION", "v18")


def _api_url(path: str) -> str:
    return f"{_base()}/{_version()}{path}"


def _developer_token() -> str:
    dev = getattr(settings, "GOOGLE_ADS_DEVELOPER_TOKEN", "")
    if not dev:
        raise ConnectorError("Google Ads developer token is not configured on the server.")
    return dev


def _clean_cid(raw: str) -> str:
    """Strip dashes/spaces from a customer id -> digit string."""
    cid = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not cid:
        raise ConnectorError("Not connected: missing customer_id.")
    return cid


async def _access_token(conn: Connection, db) -> str:
    creds = conn.creds()
    rt = creds.get("refresh_token")
    if not rt:
        raise ConnectorError("Not connected: missing refresh token.")
    token_uri = getattr(settings, "GOOGLE_OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token")
    try:
        res = await http_post(
            token_uri,
            data={
                "client_id": getattr(settings, "GOOGLE_CLIENT_ID", ""),
                "client_secret": getattr(settings, "GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": rt,
                "grant_type": "refresh_token",
            },
        )
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code != 200:
        raise ConnectorError(f"token refresh failed {res.status_code}: {res.text[:300]}")
    return res.json()["access_token"]


def _headers(conn: Connection, token: str, login_customer_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": _developer_token(),
        "Content-Type": "application/json",
    }
    login_cid = login_customer_id or conn.creds().get("login_customer_id")
    if login_cid:
        headers["login-customer-id"] = _clean_cid(login_cid)
    return headers


async def _discover_customer_id(conn: Connection, db) -> str:
    """First Google Ads customer accessible by this OAuth token. Cached LONG."""
    async def _loader() -> str:
        token = await _access_token(conn, db)
        url = _api_url("/customers:listAccessibleCustomers")
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_headers(conn, token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"listAccessibleCustomers unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(
                f"listAccessibleCustomers failed {res.status_code}: {res.text[:300]}"
            )
        names = (res.json() or {}).get("resourceNames", [])
        if not names:
            raise ConnectorError("No Google Ads customers accessible on this account.")
        return names[0].split("/")[-1]

    return await cached(
        "google_keywords", conn.id, "discover_customer_id", TTL_LONG, _loader, args={}
    )


async def _resolve_customer(conn: Connection, db, customer_id) -> str:
    if customer_id:
        return _clean_cid(customer_id)
    creds_cid = conn.creds().get("customer_id")
    if creds_cid:
        return _clean_cid(creds_cid)
    return await _discover_customer_id(conn, db)


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def _micros_to_currency(micros) -> float | None:
    if micros in (None, "", 0, "0"):
        return None
    try:
        return round(int(micros) / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


def _format_idea_row(row: dict) -> dict:
    m = row.get("keywordIdeaMetrics") or {}
    monthly = []
    for mv in m.get("monthlySearchVolumes") or []:
        monthly.append({
            "year": int(mv.get("year", 0) or 0),
            "month": mv.get("month"),
            "month_index": _MONTH_INDEX.get(mv.get("month", ""), 0),
            "searches": int(mv.get("monthlySearches") or 0),
        })
    return {
        "keyword": row.get("text", ""),
        "avg_monthly_searches": int(m.get("avgMonthlySearches") or 0),
        "competition": m.get("competition", "UNSPECIFIED"),
        "competition_index": int(m.get("competitionIndex") or 0),  # 0-100
        "low_top_of_page_bid": _micros_to_currency(m.get("lowTopOfPageBidMicros")),
        "high_top_of_page_bid": _micros_to_currency(m.get("highTopOfPageBidMicros")),
        "monthly_searches": monthly,
    }


def _coerce_seeds(args: dict) -> list[str]:
    """Accept seeds in many shapes: seeds=[...] or seed='x' or keywords=[...]."""
    raw = args.get("seeds") or args.get("seed") or args.get("keywords") or args.get("keyword")
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        out = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


# --------------------------------------------------------------------------- #
# Core API calls
# --------------------------------------------------------------------------- #
async def _generate_keyword_ideas_raw(
    conn: Connection,
    db,
    *,
    customer_id: str,
    seeds: list[str],
    page_token: str | None,
    page_size: int,
    geos: list[str],
    language: str,
    include_adult: bool,
    login_customer_id: str | None = None,
) -> dict:
    """Single raw call to KeywordPlanIdeaService.GenerateKeywordIdeas."""
    token = await _access_token(conn, db)
    url = _api_url(f"/customers/{customer_id}:generateKeywordIdeas")
    body: dict = {
        "language": language,
        "geoTargetConstants": geos,
        "keywordPlanNetwork": "GOOGLE_SEARCH",
        "includeAdultKeywords": bool(include_adult),
        "pageSize": max(1, min(int(page_size or 25), 200)),
        "keywordSeed": {"keywords": seeds[:20]},
    }
    if page_token:
        body["pageToken"] = page_token

    try:
        async with limit_for(url):
            res = await http_post(
                url,
                headers=_headers(conn, token, login_customer_id=login_customer_id or customer_id),
                json=body,
            )
    except UpstreamUnavailable as e:
        raise ConnectorError(f"generateKeywordIdeas unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"generateKeywordIdeas {res.status_code}: {res.text[:300]}")
    return res.json() or {}


def _max_year_month_range() -> dict:
    """Widest valid Keyword Planner history: ~4 years ending at the last complete
    month. Google caps history at 4 years and rejects ranges outside it; if
    `year_month_range` is unset the API returns only the past 12 months."""
    today = date.today()
    end = today.replace(day=1) - timedelta(days=1)   # last day of previous month
    idx = end.year * 12 + (end.month - 1) - 47        # 47 months back (~4yr window)
    sy, sm = divmod(idx, 12)
    return {
        "start": {"year": sy, "month": _MONTH_ENUM[sm]},
        "end": {"year": end.year, "month": _MONTH_ENUM[end.month - 1]},
    }


async def _generate_historical_metrics_raw(
    conn: Connection,
    db,
    *,
    customer_id: str,
    keywords: list[str],
    geos: list[str],
    language: str,
    login_customer_id: str | None = None,
) -> dict:
    """Single raw call to KeywordPlanIdeaService.GenerateKeywordHistoricalMetrics.

    Requests Google's widest historical window (~4 years) so seasonal/volume
    tools aren't stuck on the 12-month default. If that exact range is rejected
    (rare account/locale quirks), it retries once without the range — degrading
    to the 12-month default rather than failing the call."""
    token = await _access_token(conn, db)
    url = _api_url(f"/customers/{customer_id}:generateKeywordHistoricalMetrics")
    base_body = {
        "keywords": [k for k in keywords if k][:30],
        "language": language,
        "geoTargetConstants": geos,
        "keywordPlanNetwork": "GOOGLE_SEARCH",
    }

    async def _post(body: dict):
        try:
            async with limit_for(url):
                return await http_post(
                    url,
                    headers=_headers(conn, token, login_customer_id=login_customer_id or customer_id),
                    json=body,
                )
        except UpstreamUnavailable as e:
            raise ConnectorError(f"generateKeywordHistoricalMetrics unavailable: {e}")

    res = await _post(
        {**base_body, "historicalMetricsOptions": {"yearMonthRange": _max_year_month_range()}}
    )
    if res.status_code >= 400:
        res = await _post(base_body)  # fall back to the API default (last 12 months)
    if res.status_code >= 400:
        raise ConnectorError(
            f"generateKeywordHistoricalMetrics {res.status_code}: {res.text[:300]}"
        )
    return res.json() or {}


# ============================================================
# Tool implementations
# ============================================================
async def keyword_ideas(conn: Connection, db, args: dict) -> dict:
    """Generate keyword ideas from seed keywords."""
    seeds = _coerce_seeds(args)
    if not seeds:
        raise ConnectorError("Provide at least one seed via `seeds` or `seed`.")

    customer_id = await _resolve_customer(conn, db, args.get("customer_id"))
    geos = _geo_constants(args.get("country") or args.get("geo") or args.get("countries"))
    lang = _lang_constant(args.get("language") or args.get("lang"))
    page_size = int(args.get("page_size") or args.get("limit") or 25)
    page_token = args.get("page_token")
    include_adult = bool(args.get("include_adult", False))

    async def _loader() -> dict:
        data = await _generate_keyword_ideas_raw(
            conn,
            db,
            customer_id=customer_id,
            seeds=seeds,
            page_token=page_token,
            page_size=page_size,
            geos=geos,
            language=lang,
            include_adult=include_adult,
            login_customer_id=args.get("login_customer_id"),
        )
        rows = [_format_idea_row(r) for r in data.get("results", [])]
        return {
            "keywords": rows,
            "total_size": int(data.get("totalSize") or len(rows)),
            "next_page_token": data.get("nextPageToken"),
            "customer_id": customer_id,
            "geo_targets": geos,
            "language": lang,
        }

    return await cached(
        "google_keywords",
        conn.id,
        "keyword_ideas",
        TTL_MEDIUM,
        _loader,
        args={
            "seeds": sorted(seeds),
            "geos": geos,
            "lang": lang,
            "page_size": page_size,
            "page_token": page_token,
            "include_adult": include_adult,
            "customer_id": customer_id,
        },
    )


async def keyword_volumes(conn: Connection, db, args: dict) -> dict:
    """Historical monthly search volumes for a list of keywords."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords` (list of strings).")

    customer_id = await _resolve_customer(conn, db, args.get("customer_id"))
    geos = _geo_constants(args.get("country") or args.get("geo"))
    lang = _lang_constant(args.get("language") or args.get("lang"))

    async def _loader() -> dict:
        data = await _generate_historical_metrics_raw(
            conn, db, customer_id=customer_id, keywords=keywords, geos=geos, language=lang,
            login_customer_id=args.get("login_customer_id"),
        )
        out = []
        for r in data.get("results", []):
            out.append(
                _format_idea_row(
                    {
                        "text": r.get("text"),
                        "keywordIdeaMetrics": r.get("keywordMetrics")
                        or r.get("keywordIdeaMetrics")
                        or {},
                    }
                )
            )
        return {
            "keywords": out,
            "customer_id": customer_id,
            "geo_targets": geos,
            "language": lang,
        }

    return await cached(
        "google_keywords",
        conn.id,
        "keyword_volumes",
        TTL_LONG,
        _loader,
        args={"keywords": sorted(keywords), "geos": geos, "lang": lang, "customer_id": customer_id},
    )


async def related_keywords(conn: Connection, db, args: dict) -> dict:
    """Semantically related keywords — uses keyword_ideas with default page size."""
    args = {**args, "page_size": args.get("page_size") or 50}
    return await keyword_ideas(conn, db, args)


async def long_tail_suggestions(conn: Connection, db, args: dict) -> dict:
    """Long-tail variations — keyword_ideas filtered to >= min_words tokens."""
    min_words = max(2, int(args.get("min_words") or 3))
    base = await keyword_ideas(conn, db, {**args, "page_size": args.get("page_size") or 100})
    long_tail = [r for r in base["keywords"] if len((r["keyword"] or "").split()) >= min_words]
    return {**base, "keywords": long_tail, "min_words": min_words}


async def competitive_density(conn: Connection, db, args: dict) -> dict:
    """How crowded each keyword is. Reads competition fields from keyword_volumes."""
    data = await keyword_volumes(conn, db, args)
    bucket: dict[str, int] = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "UNSPECIFIED": 0}
    out = []
    for r in data["keywords"]:
        comp = r.get("competition", "UNSPECIFIED")
        bucket[comp] = bucket.get(comp, 0) + 1
        out.append({
            "keyword": r["keyword"],
            "competition": comp,
            "competition_index": r["competition_index"],
            "low_top_of_page_bid": r["low_top_of_page_bid"],
            "high_top_of_page_bid": r["high_top_of_page_bid"],
            "avg_monthly_searches": r["avg_monthly_searches"],
        })
    return {"keywords": out, "distribution": bucket, "customer_id": data["customer_id"]}


async def seasonal_trends(conn: Connection, db, args: dict) -> dict:
    """Monthly volume curve per keyword (up to ~4 years). Pulls historical metrics and reshapes."""
    data = await keyword_volumes(conn, db, args)
    out = []
    for r in data["keywords"]:
        monthly = r.get("monthly_searches") or []
        monthly_sorted = sorted(monthly, key=lambda x: (x["year"], x["month_index"]))
        if monthly_sorted:
            vols = [m["searches"] for m in monthly_sorted]
            peak_idx = max(range(len(vols)), key=lambda i: vols[i])
            trough_idx = min(range(len(vols)), key=lambda i: vols[i])
            avg = sum(vols) / len(vols) if vols else 0
            variance = (max(vols) - min(vols)) / avg if avg else 0
        else:
            peak_idx = trough_idx = -1
            variance = 0
        out.append({
            "keyword": r["keyword"],
            "avg_monthly_searches": r["avg_monthly_searches"],
            "monthly": monthly_sorted,
            "peak_month": monthly_sorted[peak_idx] if peak_idx >= 0 else None,
            "trough_month": monthly_sorted[trough_idx] if trough_idx >= 0 else None,
            "seasonality_index": round(variance, 2),
        })
    return {"keywords": out, "customer_id": data["customer_id"]}


async def geo_volume(conn: Connection, db, args: dict) -> dict:
    """Search volume per geo for one keyword (or short list). Calls historical metrics
    once per geo and stitches the results."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords` or `keyword`.")
    countries = args.get("countries") or args.get("geos") or ["US", "UK", "CA", "AU", "IN"]
    if isinstance(countries, str):
        countries = [c.strip() for c in countries.split(",") if c.strip()]

    customer_id = await _resolve_customer(conn, db, args.get("customer_id"))
    lang = _lang_constant(args.get("language") or args.get("lang"))

    async def _loader() -> dict:
        rows: list[dict] = []
        for c in list(countries)[:10]:
            geos = _geo_constants(c)
            if not geos:
                continue
            try:
                data = await _generate_historical_metrics_raw(
                    conn, db, customer_id=customer_id, keywords=keywords, geos=geos, language=lang
                )
            except ConnectorError as e:
                rows.append({"country": c, "error": str(e)[:200]})
                continue
            for r in data.get("results", []):
                m = r.get("keywordMetrics") or r.get("keywordIdeaMetrics") or {}
                rows.append({
                    "country": c,
                    "keyword": r.get("text"),
                    "avg_monthly_searches": int(m.get("avgMonthlySearches") or 0),
                    "competition": m.get("competition", "UNSPECIFIED"),
                    "low_top_of_page_bid": _micros_to_currency(m.get("lowTopOfPageBidMicros")),
                    "high_top_of_page_bid": _micros_to_currency(m.get("highTopOfPageBidMicros")),
                })
        return {"by_geo": rows, "keywords": keywords, "customer_id": customer_id}

    return await cached(
        "google_keywords",
        conn.id,
        "geo_volume",
        TTL_LONG,
        _loader,
        args={
            "keywords": sorted(keywords),
            "countries": list(countries)[:10],
            "lang": lang,
            "customer_id": customer_id,
        },
    )


# ---------- Heuristic tools (no API call) ----------
_INTENT_PATTERNS = [
    ("transactional", re.compile(r"\b(buy|order|purchase|deal|coupon|discount|price|cheap|hire|book|signup|sign\s*up|subscribe|download|install|free\s*trial)\b", re.I)),
    ("commercial",     re.compile(r"\b(best|top|vs|versus|review|reviews|compare|comparison|alternative|alternatives|software|tool|tools|service|services|companies|provider|providers)\b", re.I)),
    ("navigational",   re.compile(r"\b(login|log\s*in|sign\s*in|dashboard|support|contact|customer\s*service|wikipedia|facebook|youtube|gmail)\b", re.I)),
    ("informational",  re.compile(r"\b(how|what|why|when|where|guide|tutorial|examples?|tips|ideas?|meaning|definition|learn)\b", re.I)),
]


async def intent_classify(conn: Connection, db, args: dict) -> dict:
    """Classify keywords by search intent — heuristic, no API call."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords` or `keyword`.")
    out = []
    for kw in keywords:
        matched = "informational"  # default
        for label, pat in _INTENT_PATTERNS:
            if pat.search(kw):
                matched = label
                break
        out.append({"keyword": kw, "intent": matched})
    distribution: dict[str, int] = defaultdict(int)
    for r in out:
        distribution[r["intent"]] += 1
    return {"keywords": out, "distribution": dict(distribution)}


_NEGATIVE_GENERIC = [
    "free", "cheap", "diy", "vs", "review", "reviews", "alternatives",
    "wikipedia", "definition", "meaning", "what is", "what are",
    "jobs", "career", "salary", "internship", "course", "tutorial",
    "youtube", "reddit", "torrent", "crack", "download",
]


async def negative_suggestions(conn: Connection, db, args: dict) -> dict:
    """Suggest negative keywords for a campaign — generic list + heuristic from seeds.
    `brand` is optional; if provided, common competitor/comparison patterns are
    surfaced."""
    seeds = _coerce_seeds(args)
    brand = (args.get("brand") or "").strip().lower()
    out: list[dict] = []
    for term in _NEGATIVE_GENERIC:
        out.append({"keyword": term, "match_type": "phrase", "reason": "low-intent / informational"})

    if brand:
        out.append({"keyword": brand, "match_type": "phrase", "reason": "brand isolation (avoid cannibalizing organic)"})
        out.append({"keyword": f"{brand} alternative", "match_type": "phrase", "reason": "competitor-shopping intent"})
        out.append({"keyword": f"{brand} vs", "match_type": "phrase", "reason": "comparison intent"})
        out.append({"keyword": f"{brand} review", "match_type": "phrase", "reason": "research, not buying"})

    return {"negatives": out, "seeds": seeds, "brand": brand or None}


async def group_keywords(conn: Connection, db, args: dict) -> dict:
    """Cluster keywords into themed ad groups by shared significant tokens.
    Heuristic: use the longest non-stopword token as the cluster key."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords`.")

    stopwords = {
        "a", "an", "the", "for", "of", "to", "in", "on", "at", "by", "and", "or",
        "is", "are", "with", "without", "near", "me", "my", "your", "best", "top",
        "vs", "how", "what", "why",
    }
    buckets: dict[str, list[str]] = defaultdict(list)
    for kw in keywords:
        tokens = [t for t in re.findall(r"\w+", kw.lower()) if t and t not in stopwords]
        if not tokens:
            buckets["misc"].append(kw)
            continue
        # Group on the longest meaningful token — usually the head term.
        head = max(tokens, key=len)
        buckets[head].append(kw)

    groups = [
        {"name": name.title(), "keywords": sorted(set(items)), "size": len(items)}
        for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    ]
    return {"groups": groups, "total_keywords": len(keywords)}


async def keyword_forecast(conn: Connection, db, args: dict) -> dict:
    """Forecasted clicks/impressions/cost. Requires creating a KeywordPlan +
    PlanCampaigns + AdGroups + Keywords on Google Ads, which is multi-call and
    mutates state. Not implemented in v1 — falls back to a heuristic estimate
    from `keyword_volumes` so callers get something useful."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords`.")
    bid = float(args.get("bid") or 1.5)  # USD
    vols = await keyword_volumes(conn, db, args)
    out = []
    for r in vols["keywords"]:
        searches = r["avg_monthly_searches"]
        # Very rough heuristic CTR by competition tier
        ctr_table = {"LOW": 0.06, "MEDIUM": 0.045, "HIGH": 0.03, "UNSPECIFIED": 0.04}
        ctr = ctr_table.get(r["competition"], 0.04)
        impressions = int(searches * 0.4)  # share of voice estimate
        clicks = int(impressions * ctr)
        cost = round(clicks * bid, 2)
        out.append({
            "keyword": r["keyword"],
            "est_impressions": impressions,
            "est_clicks": clicks,
            "est_cost": cost,
            "est_ctr": round(ctr * 100, 2),
            "bid": bid,
        })
    return {
        "forecast": out,
        "method": "heuristic_v1",
        "disclaimer": "Heuristic estimate. For true forecasts, use Google Ads Keyword Planner directly.",
    }


async def device_volume(conn: Connection, db, args: dict) -> dict:
    """Per-device search volume isn't surfaced by Keyword Planner API. Returns a
    rough heuristic split based on industry averages."""
    keywords = _coerce_seeds(args)
    if not keywords:
        raise ConnectorError("Provide `keywords`.")
    vols = await keyword_volumes(conn, db, args)
    # Industry-average device split (US, all categories). Override per-vertical via args.
    split = args.get("split") or {"mobile": 0.62, "desktop": 0.33, "tablet": 0.05}
    rows = []
    for r in vols["keywords"]:
        total = r["avg_monthly_searches"]
        rows.append({
            "keyword": r["keyword"],
            "total": total,
            "mobile": int(total * split.get("mobile", 0.62)),
            "desktop": int(total * split.get("desktop", 0.33)),
            "tablet": int(total * split.get("tablet", 0.05)),
        })
    return {
        "keywords": rows,
        "split_assumption": split,
        "disclaimer": "Heuristic split — Keyword Planner does not return device breakdowns.",
    }


# --------------------------------------------------------------------------- #
# Catalog + registration
# --------------------------------------------------------------------------- #
_CUSTOMER_ID_PROP = {
    "type": "string",
    "description": "Google Ads customer id (10 digits, dashes ok). Defaults to the connected account or first accessible customer.",
}
_LANGUAGE_PROP = {
    "type": "string",
    "description": "ISO language code (e.g. 'en') or languageConstant id. Default 'en' (1000).",
}
_COUNTRY_PROP = {
    "type": "string",
    "description": "ISO country code (e.g. 'US') or geoTargetConstant id. Default 'US' (2840).",
}
_KEYWORDS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Keyword strings. May also be passed as a comma-separated string.",
}

CATALOG = {
    "keyword_ideas": {
        "description": "Keyword Planner: ideas from seed keywords with monthly searches, competition, CPC.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "seeds": _KEYWORDS_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
                "page_size": {"type": "integer", "description": "Max ideas to return (1-200), default 25."},
                "page_token": {"type": "string", "description": "Pagination token from a prior call's next_page_token."},
                "include_adult": {"type": "boolean", "description": "Include adult keywords. Default false."},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    "keyword_volumes": {
        "description": "Historical search volumes for a list of keywords.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "keyword_forecast": {
        "description": "Forecasted clicks, impressions, cost for keywords at a given bid.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
                "bid": {"type": "number", "description": "Max CPC bid in USD used for cost estimate. Default 1.5."},
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "related_keywords": {
        "description": "Semantically related keywords for a seed term.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "seeds": _KEYWORDS_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
                "page_size": {"type": "integer", "description": "Max ideas to return (1-200), default 50."},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    "long_tail_suggestions": {
        "description": "Long-tail variations grouped by intent.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "seeds": _KEYWORDS_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
                "min_words": {"type": "integer", "description": "Minimum word count for a long-tail keyword. Default 3."},
                "page_size": {"type": "integer", "description": "Max ideas to scan (1-200), default 100."},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    "competitive_density": {
        "description": "How crowded a keyword is — Google Ads competition + density.",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "seasonal_trends": {
        "description": "Monthly volume swings over up to ~4 years (Google Keyword Planner max).",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "geo_volume": {
        "description": "Search volume by geo target (country, region, city).",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "countries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO country codes or geoTargetConstant ids. Default ['US','UK','CA','AU','IN'].",
                },
                "language": _LANGUAGE_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "device_volume": {
        "description": "Search volume split by device (mobile, desktop, tablet).",
        "input": {
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_PROP,
                "keywords": _KEYWORDS_PROP,
                "country": _COUNTRY_PROP,
                "language": _LANGUAGE_PROP,
                "split": {
                    "type": "object",
                    "description": "Override device split fractions, e.g. {\"mobile\":0.62,\"desktop\":0.33,\"tablet\":0.05}.",
                },
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "intent_classify": {
        "description": "Classify keywords by search intent: informational, navigational, transactional, commercial.",
        "input": {
            "type": "object",
            "properties": {
                "keywords": _KEYWORDS_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
    "negative_suggestions": {
        "description": "Suggest negative keywords from a brief or seed list.",
        "input": {
            "type": "object",
            "properties": {
                "seeds": _KEYWORDS_PROP,
                "keywords": _KEYWORDS_PROP,
                "brand": {"type": "string", "description": "Optional brand name to surface competitor/comparison negatives."},
            },
            "required": [],
            "additionalProperties": True,
        },
    },
    "group_keywords": {
        "description": "Cluster keywords into themed ad groups.",
        "input": {
            "type": "object",
            "properties": {
                "keywords": _KEYWORDS_PROP,
            },
            "required": ["keywords"],
            "additionalProperties": True,
        },
    },
}

HANDLERS = {
    "keyword_ideas": keyword_ideas,
    "keyword_volumes": keyword_volumes,
    "keyword_forecast": keyword_forecast,
    "related_keywords": related_keywords,
    "long_tail_suggestions": long_tail_suggestions,
    "competitive_density": competitive_density,
    "seasonal_trends": seasonal_trends,
    "geo_volume": geo_volume,
    "device_volume": device_volume,
    "intent_classify": intent_classify,
    "negative_suggestions": negative_suggestions,
    "group_keywords": group_keywords,
}

registry.register(
    Connector(
        slug="google_keywords",
        label="Google Keyword Planner",
        auth="google_oauth",
        scopes=["https://www.googleapis.com/auth/adwords"],
        catalog=CATALOG,
        handlers=HANDLERS,
        description="Keyword Planner ideas, historical search volumes and keyword research via the Google Ads API.",
        category="Advertising",
    )
)
