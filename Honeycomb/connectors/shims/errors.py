"""Connector error types **and** the credential-redaction helpers every error path uses.

Why redaction lives next to the error type: an MCP tool error is echoed verbatim to
the AI client (and stored in the user-visible activity log), and the strings that
reach it routinely carry secrets:

  * Meta/Graph passes the access token as a QUERY PARAM, and paging follows
    ``paging.next`` URLs that embed ``access_token=EAA…``; AWR export URLs are the
    same shape. Any exception that names the URL leaks a live credential.
  * Upstream 4xx bodies echo back the ``Authorization`` header or the key we sent.
  * Honeycomb's own MCP bearer tokens (``hc_…``) arrive in that same header, so a
    handler that dumps its request headers would otherwise mint a working key into
    the activity log.

So: **no URL and no upstream message may reach an exception, a log line, a stored
column, or a JSON-RPC response without going through ``redact_url`` /
``redact_text`` first.** These are deliberately conservative — they over-mask
rather than risk printing a token, because an error message is a debugging aid,
never an authoritative record.
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = '***'


class ConnectorError(Exception):
    """Raised by a connector tool handler when an upstream/usage error occurs."""


# ---------------------------------------------------------------- URL redaction
# Query-parameter names whose VALUE is a secret. Matched against the whole name
# and against each alphanumeric component of it, so "access_token", "api_key",
# "X-Api-Key" and "refresh_token" all hit while "keyword"/"keys" do not.
_SENSITIVE_PARAMS = frozenset({
    'access_token', 'token', 'key', 'api_key', 'apikey', 'secret', 'password',
    'signature', 'sig', 'auth',
})
_NAME_PARTS = re.compile(r'[^a-z0-9]+')


def _is_sensitive_param(name: str) -> bool:
    low = name.strip().lower()
    if low in _SENSITIVE_PARAMS:
        return True
    return any(p in _SENSITIVE_PARAMS for p in _NAME_PARTS.split(low) if p)


def _redact_query(query: str) -> str:
    """Mask the values of sensitive params, keeping every param NAME intact."""
    if not query or '=' not in query:
        return query
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return query
    if not pairs:
        return query
    # safe='*' keeps the mask readable as ``name=***`` instead of ``name=%2A%2A%2A``.
    return urlencode([(k, MASK if _is_sensitive_param(k) else v) for k, v in pairs], safe='*')


def redact_url(url: str) -> str:
    """Return ``url`` with credential-bearing query params (and any userinfo) masked.

    The scheme, host and PATH are preserved so the message still says which
    endpoint failed — only the secret values are replaced with ``***``.
    """
    if not url:
        return url
    text = str(url)
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    netloc = parts.netloc
    if '@' in netloc:  # https://user:pass@host — the whole userinfo is a credential
        netloc = f'{MASK}@{netloc.rpartition("@")[2]}'
    fragment = _redact_query(parts.fragment) if '=' in parts.fragment else parts.fragment
    return urlunsplit((parts.scheme, netloc, parts.path, _redact_query(parts.query), fragment))


# --------------------------------------------------------------- text redaction
_URL_IN_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>\\^`{|}]+")
# "Bearer <tok>" / "Basic <tok>" anywhere in a message or echoed header dump.
_BEARER = re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}')
# This repo's (and its upstreams') key prefixes — keep the prefix, drop the rest.
# ``hc_`` is Honeycomb's own McpKey prefix; the rest are inherited from falcon
# because the same upstreams are being talked to.
_PREFIXED_KEY = re.compile(
    r'\b(hc_|tsc_|fmcp_|fsh_|ghp_|gho_|ghs_|ghu_|ghr_|sk-|gsk_|xai-|AIza)[A-Za-z0-9_-]{4,}')
# Meta/Facebook user + page access tokens.
_META_TOKEN = re.compile(r'\bEAA[A-Za-z0-9]{12,}')
# JWTs (the portal's own access/refresh cookies, Google id_tokens, …).
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+')
# key=value / "key": "value" pairs in an echoed JSON or form body.
_KV_SECRET = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|apikey|"
    r"client[_-]?secret|secret|password|passwd|signature|auth[_-]?token|token)"
    r"(\"?\s*[=:]\s*\"?)[^\s\"',&}\]]+")
# Long high-entropy blobs (base64/base64url). Requires BOTH a digit and a letter so
# ordinary prose and long identifiers-with-separators are left alone.
_BLOB = re.compile(
    r'(?<![A-Za-z0-9+_=-])'
    r'(?=[A-Za-z0-9+_-]*[0-9])(?=[A-Za-z0-9+_-]*[A-Za-z])'
    r'[A-Za-z0-9+_-]{40,}={0,2}'
    r'(?![A-Za-z0-9+_=-])')


def redact_text(text) -> str:
    """Strip credentials out of an arbitrary message before it is shown or stored.

    Masks: URLs' secret query params, ``Bearer``/``Basic`` values, this repo's key
    prefixes (``hc_``/``tsc_``/``fmcp_``/``fsh_``/``ghp_``/``sk-``/``gsk_``), Meta
    ``EAA…`` tokens, JWTs, ``token=…``-style pairs, and long base64-ish blobs.
    """
    if text is None:
        return ''
    out = str(text)
    if not out:
        return out
    out = _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), out)
    out = _BEARER.sub(lambda m: f'{m.group(1)} {MASK}', out)
    out = _PREFIXED_KEY.sub(lambda m: f'{m.group(1)}{MASK}', out)
    out = _META_TOKEN.sub(f'EAA{MASK}', out)
    out = _JWT.sub(MASK, out)
    out = _KV_SECRET.sub(lambda m: f'{m.group(1)}{m.group(2)}{MASK}', out)
    return _BLOB.sub(MASK, out)


def redact_exc(exc: BaseException) -> str:
    """``'TypeName: message'`` with the message redacted — the standard MCP error text."""
    return f'{type(exc).__name__}: {redact_text(exc)}'
