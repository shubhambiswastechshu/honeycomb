"""Microsoft Clarity connector — Data Export API (project live insights).

Clarity exposes a single Data Export endpoint that returns aggregated behavioural
metrics (Traffic, ScrollDepth, EngagementTime, ...) for the last N days, optionally
broken down by up to three dimensions (OS, Browser, Device, Country, etc.).

Docs: https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export

Auth: api_key — the user pastes their per-project Clarity API token (a JWT generated
in the Clarity dashboard under Settings -> Data Export). Sent as a Bearer token.

Note: Clarity hard-limits the export API to a handful of calls per project per day, so
4xx responses (esp. 401 invalid token and 429 rate limit) are surfaced verbatim.

Most tools below hit the same project-live-insights endpoint but with a different
dimension breakdown (Country, Device, Browser, OS, ReferrerUrl, URL, ...). Ported
faithfully from the RAVEN ms_clarity reference (services.py / tools.py).
"""
from connectors import registry
from connectors.registry import Connector
from connections.models import Connection
from connectors.shims.errors import ConnectorError
from connectors.shims.http import get as http_get, UpstreamUnavailable
from connectors.shims.cache import cached, TTL_MEDIUM
from connectors.shims.concurrency import limit_for

EXPORT_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

# Clarity only accepts these dimension names (case-sensitive in the API).
_VALID_DIMENSIONS = {
    "OS",
    "Browser",
    "Device",
    "Country",
    "PageTitle",
    "ReferrerUrl",
    "DeviceFamily",
    "Source",
    "Medium",
    "Campaign",
    "Channel",
    "URL",
}


def _token(conn: Connection) -> str:
    creds = conn.creds()
    token = creds.get("api_token")
    if not token:
        raise ConnectorError("Not connected: missing api_token.")
    return token


def _validate_dimension(value, label):
    if value is None or value == "":
        return None
    if value not in _VALID_DIMENSIONS:
        raise ConnectorError(
            f"Invalid {label} '{value}'. Allowed: {', '.join(sorted(_VALID_DIMENSIONS))}."
        )
    return value


def _num_of_days(args: dict) -> int:
    """Clarity caps numOfDays at 1..3 (mirrors RAVEN's max(1, min(int(...), 3)))."""
    raw = args.get("num_of_days")
    if raw is None:
        raw = args.get("days")
    try:
        n = int(raw) if raw is not None else 3
    except (TypeError, ValueError):
        raise ConnectorError("num_of_days must be an integer (1, 2, or 3).")
    return max(1, min(n, 3))


def _coerce_dims(args: dict, default):
    """Resolve caller-supplied dimensions, falling back to a tool default.

    Accepts `dimensions` as a list or comma string, or the legacy `dimension`
    alias. Clarity accepts at most 3 dimensions per call.
    """
    raw = args.get("dimensions")
    if raw is None:
        raw = args.get("dimension")
    if raw is None:
        raw = default
    if isinstance(raw, str):
        dims = [d.strip() for d in raw.split(",") if d.strip()]
    elif isinstance(raw, (list, tuple)):
        dims = [str(d).strip() for d in raw if str(d).strip()]
    else:
        dims = list(default)
    out = []
    for d in dims[:3]:
        out.append(_validate_dimension(d, "dimension"))
    return [d for d in out if d]


async def _fetch_insights(conn: Connection, tool: str, num_of_days: int, dimensions):
    """Single cached call to Clarity's project-live-insights endpoint."""
    token = _token(conn)

    params = {"numOfDays": num_of_days}
    if dimensions:
        for i, d in enumerate(dimensions[:3], start=1):
            params[f"dimension{i}"] = d

    async def _loader():
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with limit_for(EXPORT_URL):
                res = await http_get(EXPORT_URL, headers=headers, params=params)
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))

        if res.status_code in (401, 403):
            raise ConnectorError(
                "Clarity rejected the token. Regenerate it in Clarity -> "
                "Project Settings -> Data Export API."
            )
        if res.status_code >= 400:
            raise ConnectorError(
                f"Clarity export failed {res.status_code}: {res.text[:600]}"
            )

        try:
            payload = res.json()
        except ValueError:
            raise ConnectorError(
                f"Clarity returned non-JSON response: {res.text[:600]}"
            )

        metrics = payload if isinstance(payload, list) else [payload]
        return {
            "num_of_days": num_of_days,
            "dimensions": list(dimensions or []),
            "metric_count": len(metrics),
            "metrics": metrics,
        }

    return await cached("ms_clarity", conn.id, tool, TTL_MEDIUM, _loader, args=params)


def _project_id(conn: Connection, args: dict):
    """Resolve a project id from args first, then creds (best-effort)."""
    pid = args.get("project_id") or args.get("project")
    if not pid:
        creds = conn.creds()
        pid = creds.get("project_id") or creds.get("projectId")
    return str(pid).strip() if pid else None


# ============================================================
# Tool implementations
# ============================================================

async def list_projects(conn: Connection, db, args: dict) -> dict:
    """List the Clarity project(s) reachable with this token.

    BRING-DATA stores a single per-project Data Export token, so this reflects the
    project bound to this connection (id from creds, if configured).
    """
    creds = conn.creds()
    pid = creds.get("project_id") or creds.get("projectId")
    name = creds.get("project_name") or creds.get("name")
    projects = []
    if pid or name:
        projects.append(
            {
                "project_id": str(pid) if pid else None,
                "name": name or (str(pid) if pid else None),
                "display_name": name or (str(pid) if pid else None),
            }
        )
    return {"projects": projects}


async def get_metrics(conn: Connection, db, args: dict) -> dict:
    """Top-line Clarity live-insights metrics (no dimension breakdown).

    The caller may still pass dimension1/2/3 (legacy) to break down results.
    """
    num_of_days = _num_of_days(args)
    # Backward-compatible: honour explicit dimension1/2/3 if supplied.
    explicit = []
    for key in ("dimension1", "dimension2", "dimension3"):
        d = _validate_dimension(args.get(key), key)
        if d:
            explicit.append(d)
    dims = explicit or _coerce_dims(args, [])
    return await _fetch_insights(conn, "get_metrics", num_of_days, dims)


async def top_pages(conn: Connection, db, args: dict) -> dict:
    """Top pages by sessions and engagement signals (URL breakdown)."""
    return await _fetch_insights(conn, "top_pages", _num_of_days(args), ["URL"])


async def rage_clicks(conn: Connection, db, args: dict) -> dict:
    """Pages with the most rage clicks (URL breakdown)."""
    return await _fetch_insights(conn, "rage_clicks", _num_of_days(args), ["URL"])


async def dead_clicks(conn: Connection, db, args: dict) -> dict:
    """Pages with the most dead clicks (URL breakdown)."""
    return await _fetch_insights(conn, "dead_clicks", _num_of_days(args), ["URL"])


async def excessive_scroll(conn: Connection, db, args: dict) -> dict:
    """Sessions with excessive scrolling behaviour (URL breakdown)."""
    return await _fetch_insights(conn, "excessive_scroll", _num_of_days(args), ["URL"])


async def quick_back(conn: Connection, db, args: dict) -> dict:
    """Quick-back sessions / immediate-bounce indicators (URL breakdown)."""
    return await _fetch_insights(conn, "quick_back", _num_of_days(args), ["URL"])


async def country_breakdown(conn: Connection, db, args: dict) -> dict:
    """Sessions split by country."""
    return await _fetch_insights(conn, "country_breakdown", _num_of_days(args), ["Country"])


async def device_breakdown(conn: Connection, db, args: dict) -> dict:
    """Sessions by device type."""
    return await _fetch_insights(conn, "device_breakdown", _num_of_days(args), ["Device"])


async def browser_breakdown(conn: Connection, db, args: dict) -> dict:
    """Sessions by browser."""
    return await _fetch_insights(conn, "browser_breakdown", _num_of_days(args), ["Browser"])


async def os_breakdown(conn: Connection, db, args: dict) -> dict:
    """Sessions by operating system."""
    return await _fetch_insights(conn, "os_breakdown", _num_of_days(args), ["OS"])


async def referrer_breakdown(conn: Connection, db, args: dict) -> dict:
    """Top referring sources (ReferrerUrl breakdown)."""
    return await _fetch_insights(conn, "referrer_breakdown", _num_of_days(args), ["ReferrerUrl"])


async def session_filters(conn: Connection, db, args: dict) -> dict:
    """Search sessions by filter — caller-configurable dimensions (default URL+Device)."""
    dims = _coerce_dims(args, ["URL", "Device"])
    return await _fetch_insights(conn, "session_filters", _num_of_days(args), dims)


async def session_recording_url(conn: Connection, db, args: dict) -> dict:
    """Build a deep-link to a Clarity session recording. Local — no API call.

    Requires `session_id`; the project id is taken from args then creds.
    """
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        raise ConnectorError("`session_id` is required.")
    pid = _project_id(conn, args)
    if not pid:
        raise ConnectorError(
            "Not connected: missing project_id (provide it in args or connection creds)."
        )
    return {
        "project_id": pid,
        "session_id": session_id,
        "url": f"https://clarity.microsoft.com/projects/view/{pid}/replay/{session_id}",
    }


# ============================================================
# Catalog
# ============================================================

_DAYS_PROP = {
    "num_of_days": {
        "type": "integer",
        "enum": [1, 2, 3],
        "default": 3,
        "description": "Number of days back to aggregate (1, 2, or 3). Clarity caps this at 3.",
    },
}

_BREAKDOWN_INPUT = {
    "type": "object",
    "properties": dict(_DAYS_PROP),
    "required": [],
    "additionalProperties": False,
}

CATALOG = {
    "list_projects": {
        "description": "List the Clarity project(s) reachable with this connection's Data Export token.",
        "input": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_metrics": {
        "description": "Microsoft Clarity live-insights top-line metrics (Traffic, ScrollDepth, EngagementTime, dead/rage clicks, ...) for the last 1-3 days, optionally broken down by up to 3 dimensions.",
        "input": {
            "type": "object",
            "properties": {
                **_DAYS_PROP,
                "dimension1": {
                    "type": "string",
                    "description": "Optional breakdown dimension (e.g. OS, Browser, Device, Country, URL, ReferrerUrl).",
                },
                "dimension2": {
                    "type": "string",
                    "description": "Optional second breakdown dimension.",
                },
                "dimension3": {
                    "type": "string",
                    "description": "Optional third breakdown dimension.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "top_pages": {
        "description": "Top pages by sessions and engagement signals (URL breakdown), last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "rage_clicks": {
        "description": "Pages with the most rage clicks over the last 1-3 days (URL breakdown).",
        "input": _BREAKDOWN_INPUT,
    },
    "dead_clicks": {
        "description": "Pages with the most dead clicks over the last 1-3 days (URL breakdown).",
        "input": _BREAKDOWN_INPUT,
    },
    "excessive_scroll": {
        "description": "Sessions with excessive scrolling behaviour over the last 1-3 days (URL breakdown).",
        "input": _BREAKDOWN_INPUT,
    },
    "quick_back": {
        "description": "Quick-back sessions (immediate bounce indicators) over the last 1-3 days (URL breakdown).",
        "input": _BREAKDOWN_INPUT,
    },
    "country_breakdown": {
        "description": "Clarity sessions split by country, last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "device_breakdown": {
        "description": "Clarity sessions by device type, last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "browser_breakdown": {
        "description": "Clarity sessions by browser, last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "os_breakdown": {
        "description": "Clarity sessions by operating system, last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "referrer_breakdown": {
        "description": "Top referring sources (ReferrerUrl breakdown), last 1-3 days.",
        "input": _BREAKDOWN_INPUT,
    },
    "session_filters": {
        "description": "Search Clarity sessions by filter with caller-configurable dimensions (default URL + Device).",
        "input": {
            "type": "object",
            "properties": {
                **_DAYS_PROP,
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to 3 Clarity dimensions to break down by (e.g. URL, Device, Country, Browser, OS, ReferrerUrl). Defaults to URL + Device.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "session_recording_url": {
        "description": "Build a deep-link URL to view a specific Clarity session recording. Local — no API call.",
        "input": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The Clarity session id to deep-link to.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Clarity project id (defaults to the one bound to this connection).",
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "list_projects": list_projects,
    "get_metrics": get_metrics,
    "top_pages": top_pages,
    "rage_clicks": rage_clicks,
    "dead_clicks": dead_clicks,
    "excessive_scroll": excessive_scroll,
    "quick_back": quick_back,
    "country_breakdown": country_breakdown,
    "device_breakdown": device_breakdown,
    "browser_breakdown": browser_breakdown,
    "os_breakdown": os_breakdown,
    "referrer_breakdown": referrer_breakdown,
    "session_filters": session_filters,
    "session_recording_url": session_recording_url,
}

registry.register(
    Connector(
        slug="ms_clarity",
        label="Microsoft Clarity",
        auth="api_key",
        cred_fields=["api_token"],
        description="Reads aggregated Microsoft Clarity behavioural metrics - traffic, rage and dead clicks, scroll depth and engagement - broken down by page, country, device, browser, OS and referrer.",
        category="Analytics",
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
