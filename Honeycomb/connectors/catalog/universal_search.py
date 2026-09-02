"""Universal Web Search connector — DuckDuckGo HTML scrape (no API key).

DuckDuckGo exposes two keyless, server-rendered search endpoints that we can scrape
with the stdlib only (no bs4 / lxml dependencies):

  - https://html.duckduckgo.com/html/  — full results page
  - https://lite.duckduckgo.com/lite/   — minimal table-based fallback

Both require a browser-like User-Agent or they return an empty / blocked page. We POST
the query (DDG's HTML endpoint accepts form POST as well as GET), then parse the result
anchors with regular expressions to extract {title, url, snippet}.

Auth: api_key with NO cred_fields — there is nothing for the user to paste. The connector
is registered as api_key simply so it slots into the shared connection model; `creds()`
is never read.

Docs (unofficial): the HTML endpoint wraps each result in
    <a ... class="result__a" href="...">Title</a>
and the snippet in
    <a ... class="result__snippet" ...>Snippet</a>
DuckDuckGo also rewrites outbound links through /l/?uddg=<urlencoded-target>, which we
unwrap back to the real destination.
"""
import html
import re
from urllib.parse import parse_qs, unquote, urlparse

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, post as http_post

HTML_URL = "https://html.duckduckgo.com/html/"
LITE_URL = "https://lite.duckduckgo.com/lite/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
}

# Result anchor on the HTML endpoint: class="result__a" href="...">Title</a>
_RESULT_A_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# Snippet anchor/text: class="result__snippet" ...>Snippet</a>
_SNIPPET_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# Lite endpoint: plain result links in the results table.
_LITE_A_RE = re.compile(
    r'<a[^>]+class="[^"]*result-link[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_LITE_SNIPPET_RE = re.compile(
    r'class="[^"]*result-snippet[^"]*"[^>]*>(?P<snippet>.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    """Drop HTML tags and unescape entities into clean plain text."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub("", text)
    return html.unescape(no_tags).strip()


def _unwrap(href: str) -> str:
    """Resolve DuckDuckGo's /l/?uddg= redirect wrapper to the real target URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    # Redirect form: .../l/?uddg=<urlencoded target>&rut=...
    if "uddg" in (parsed.query or ""):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", [None])[0]
        if target:
            return unquote(target)
    return href


def _parse_html(body: str, max_results: int) -> list[dict]:
    rows: list[dict] = []
    snippets = [_strip(m.group("snippet")) for m in _SNIPPET_RE.finditer(body)]
    for i, m in enumerate(_RESULT_A_RE.finditer(body)):
        url = _unwrap(m.group("href"))
        title = _strip(m.group("title"))
        if not url or not title:
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        rows.append({"title": title, "url": url, "snippet": snippet})
        if len(rows) >= max_results:
            break
    return rows


def _parse_lite(body: str, max_results: int) -> list[dict]:
    rows: list[dict] = []
    snippets = [_strip(m.group("snippet")) for m in _LITE_SNIPPET_RE.finditer(body)]
    for i, m in enumerate(_LITE_A_RE.finditer(body)):
        url = _unwrap(m.group("href"))
        title = _strip(m.group("title"))
        if not url or not title:
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        rows.append({"title": title, "url": url, "snippet": snippet})
        if len(rows) >= max_results:
            break
    return rows


async def _fetch(url: str, query: str) -> str:
    try:
        async with limit_for(url):
            res = await http_post(url, headers=_HEADERS, data={"q": query})
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(
            f"DuckDuckGo search failed {res.status_code}: {res.text[:600]}"
        )
    return res.text


async def search(conn: Connection, db, args: dict) -> dict:
    """Web search via DuckDuckGo. Returns up to max_results {title,url,snippet} rows."""
    query = (args.get("query") or "").strip()
    if not query:
        raise ConnectorError("query is required.")

    max_results = int(args.get("max_results") or 10)
    if max_results < 1:
        max_results = 1
    if max_results > 50:
        max_results = 50

    async def _loader():
        # Primary: the full HTML results page.
        body = await _fetch(HTML_URL, query)
        results = _parse_html(body, max_results)

        # Fallback: the lite endpoint if the HTML page parsed nothing.
        if not results:
            lite_body = await _fetch(LITE_URL, query)
            results = _parse_lite(lite_body, max_results)

        if not results:
            return {"results": [], "note": "no results parsed"}

        return {"query": query, "count": len(results), "results": results}

    return await cached(
        "universal_search",
        conn.id,
        "search",
        TTL_MEDIUM,
        _loader,
        args={"query": query, "max_results": max_results},
    )


CATALOG = {
    "search": {
        "description": "Web search via DuckDuckGo (no API key). Returns a list of {title, url, snippet} results for a query.",
        "input": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of results to return (1-50).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "search": search,
}

registry.register(
    Connector(
        slug="universal_search",
        label="Universal Web Search",
        auth="api_key",
        cred_fields=[],
        catalog=CATALOG,
        handlers=HANDLERS,
        description="Reads web search results by scraping DuckDuckGo's keyless HTML endpoints. No API key.",
        category='Search',
    )
)
