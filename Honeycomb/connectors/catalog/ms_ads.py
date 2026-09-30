"""Microsoft Advertising connector — Microsoft Advertising API v13 (Bing Ads).

Read-only. Two halves, because Microsoft splits them:

  * **Entity lookups** (accounts, campaigns, ad groups, ads, keywords) are
    single SOAP calls that answer immediately.
  * **Performance numbers** come from the Reporting service, which is
    asynchronous: submit a report request, poll for it, then download a
    ZIP whose single CSV member holds the rows. One tool call does all
    three, so the AI client never sees the machinery.

**This API is SOAP, not REST.** v13 has no REST surface for campaigns or
reporting, so the envelopes below are built and parsed by hand rather than
with a client library — adding one dependency for one connector was not worth
it. The envelope shape is fixed and documented; see
https://learn.microsoft.com/advertising/guides/ for each service.

Auth: ``api_key`` — the registry only knows ``api_key`` and ``google_oauth``,
and Microsoft's is neither, so the four secrets are pasted once and the
connector does the OAuth refresh itself against Entra:

  * ``developer_token``  from Microsoft Advertising -> Account -> Developer settings
  * ``client_id``        the Entra app registration
  * ``client_secret``    that app's secret
  * ``refresh_token``    from the one-time consent, with ``offline_access``

The access token lives an hour, so it is refreshed on demand and written back
to the connection, exactly as the LinkedIn connector does with its own.
``customer_id`` and ``account_id`` are set up after connecting and act as the
defaults for every tool.

Not implemented: writes of any kind. Nothing here creates, edits, pauses or
deletes, so no tool carries ``'write': True``.
"""
import asyncio
import csv
import io
import time
import zipfile
from xml.etree import ElementTree as ET

from asgiref.sync import sync_to_async
from django.utils import timezone

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post
from connections.models import Connection

V13 = "https://clientcenter.api.bingads.microsoft.com/Api/CustomerManagement/v13"
CUSTOMER_URL = f"{V13}/CustomerManagementService.svc"
CAMPAIGN_URL = (
    "https://campaign.api.bingads.microsoft.com/Api/Advertiser/v13/"
    "CampaignManagement/CampaignManagementService.svc"
)
REPORTING_URL = (
    "https://reporting.api.bingads.microsoft.com/Api/Advertiser/v13/"
    "Reporting/ReportingService.svc"
)

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPE = "https://ads.microsoft.com/msads.manage offline_access"

#: The SOAP namespace each service answers in.
NS_CUSTOMER = "https://bingads.microsoft.com/Customer/v13"
NS_CAMPAIGN = "https://bingads.microsoft.com/CampaignManagement/v13"
NS_REPORTING = "https://bingads.microsoft.com/Reporting/v13"
NS_ENVELOPE = "http://schemas.xmlsoap.org/soap/envelope/"

#: Refresh this long before Entra's hour is up rather than race it.
RENEW_BEFORE_SECONDS = 300

#: How long to wait for a submitted report, and how often to ask.
REPORT_TIMEOUT_SECONDS = 90
REPORT_POLL_SECONDS = 3.0

RECONNECT = (
    "Reconnect it in the Honeycomb dashboard: Microsoft Advertising needs a "
    "developer token, an Entra client id and secret, and a refresh token."
)

#: One refresh at a time per connection: a report tool fans out, and every
#: branch finding the same spent token must not each spend the refresh.
_RENEW_LOCKS: dict[int, asyncio.Lock] = {}

#: The periods Microsoft names. Anything else is treated as a custom range.
PREDEFINED_TIME = {
    "today",
    "yesterday",
    "last_seven_days",
    "this_week",
    "last_week",
    "last_four_weeks",
    "this_month",
    "last_month",
    "last_six_months",
    "this_year",
    "last_year",
}

_TIME_ENUM = {
    "today": "Today",
    "yesterday": "Yesterday",
    "last_seven_days": "LastSevenDays",
    "this_week": "ThisWeek",
    "last_week": "LastWeek",
    "last_four_weeks": "LastFourWeeks",
    "this_month": "ThisMonth",
    "last_month": "LastMonth",
    "last_six_months": "LastSixMonths",
    "this_year": "ThisYear",
    "last_year": "LastYear",
}


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def _cred(conn: Connection, name: str) -> str:
    value = (conn.creds() or {}).get(name)
    if not value:
        raise ConnectorError(f"This Microsoft Advertising connection has no {name}. {RECONNECT}")
    return str(value)


@sync_to_async
def _save_creds(conn: Connection) -> None:
    # Only the credential column: the dashboard may have renamed the connection
    # or switched a tool off since this request loaded it.
    Connection.objects.filter(pk=conn.pk).update(
        creds_enc=conn.creds_enc, updated_at=timezone.now()
    )


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _access_token(conn: Connection) -> str:
    """The connection's access token, refreshed if it is nearly spent."""
    creds = conn.creds() or {}
    token = creds.get("access_token")
    expires_at = _as_float(creds.get("expires_at"))
    if token and expires_at - time.time() > RENEW_BEFORE_SECONDS:
        return token

    lock = _RENEW_LOCKS.setdefault(conn.id, asyncio.Lock())
    async with lock:
        # Another call may have refreshed it while this one waited.
        creds = conn.creds() or {}
        if creds.get("access_token") and _as_float(creds.get("expires_at")) - time.time() > RENEW_BEFORE_SECONDS:
            return creds["access_token"]
        fresh = await _renew(conn)
        creds = dict(creds)
        creds["access_token"] = fresh["access_token"]
        creds["expires_at"] = time.time() + _as_float(fresh.get("expires_in") or 3600)
        # Entra rotates the refresh token on some tenants; keep the new one.
        if fresh.get("refresh_token"):
            creds["refresh_token"] = fresh["refresh_token"]
        conn.set_creds(creds)
        await _save_creds(conn)
        return creds["access_token"]


async def _renew(conn: Connection) -> dict:
    form = {
        "client_id": _cred(conn, "client_id"),
        "client_secret": _cred(conn, "client_secret"),
        "refresh_token": _cred(conn, "refresh_token"),
        "grant_type": "refresh_token",
        "scope": SCOPE,
    }
    try:
        async with limit_for(TOKEN_URL):
            res = await http_post(TOKEN_URL, data=form)
    except UpstreamUnavailable as exc:
        raise ConnectorError(str(exc))
    if res.status_code >= 400:
        # The body carries error_description, which names the real problem
        # (expired refresh token, wrong secret, missing consent).
        raise ConnectorError(
            f"Microsoft refused the refresh token ({res.status_code}). "
            f"{_error_description(res)} {RECONNECT}"
        )
    try:
        return res.json()
    except ValueError:
        raise ConnectorError("Microsoft's token endpoint returned a non-JSON response.")


def _error_description(res) -> str:
    try:
        payload = res.json()
    except ValueError:
        return ""
    return str(payload.get("error_description") or payload.get("error") or "")[:300]


# --------------------------------------------------------------------------- #
# SOAP
# --------------------------------------------------------------------------- #
def _escape(value) -> str:
    """XML-escape a value going into an envelope."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _ids(conn: Connection, args: dict) -> tuple[str, str]:
    """The customer and account this call is for: the args win, then the creds."""
    creds = conn.creds() or {}
    customer = args.get("customer_id") or creds.get("customer_id")
    account = args.get("account_id") or creds.get("account_id")
    if not customer:
        raise ConnectorError(
            "No customer_id. Set one on the connection, or pass customer_id to the tool."
        )
    if not account:
        raise ConnectorError(
            "No account_id. Set one on the connection, or pass account_id to the tool."
        )
    return str(customer), str(account)


async def _soap(
    conn: Connection,
    url: str,
    namespace: str,
    action: str,
    body: str,
    *,
    customer_id: str | None = None,
    account_id: str | None = None,
) -> ET.Element:
    """One SOAP call. Returns the element inside <s:Body>, or raises."""
    token = await _access_token(conn)
    creds = conn.creds() or {}
    customer = customer_id or creds.get("customer_id") or ""
    account = account_id or creds.get("account_id") or ""

    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:i="http://www.w3.org/2001/XMLSchema-instance" xmlns="{namespace}">'
        "<s:Header>"
        f"<Action mustUnderstand=\"1\">{action}</Action>"
        f"<DeveloperToken>{_escape(_cred(conn, 'developer_token'))}</DeveloperToken>"
        f"<CustomerId>{_escape(customer)}</CustomerId>"
        f"<CustomerAccountId>{_escape(account)}</CustomerAccountId>"
        f"<AuthenticationToken>{_escape(token)}</AuthenticationToken>"
        "</s:Header>"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    )
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": action,
    }
    try:
        async with limit_for(url):
            res = await http_post(url, content=envelope.encode("utf-8"), headers=headers)
    except UpstreamUnavailable as exc:
        raise ConnectorError(str(exc))

    if res.status_code in (401, 403):
        raise ConnectorError(
            f"Microsoft Advertising rejected the credentials for {action}. {RECONNECT}"
        )
    try:
        root = ET.fromstring(res.content)
    except ET.ParseError:
        raise ConnectorError(
            f"Microsoft Advertising returned unreadable XML for {action} "
            f"({res.status_code}): {res.text[:400]}"
        )

    fault = root.find(f".//{{{NS_ENVELOPE}}}Fault")
    if fault is not None:
        raise ConnectorError(f"{action} failed: {_fault_text(fault)}")
    if res.status_code >= 400:
        raise ConnectorError(
            f"{action} failed {res.status_code}: {res.text[:400]}"
        )

    body_el = root.find(f"{{{NS_ENVELOPE}}}Body")
    if body_el is None or len(body_el) == 0:
        raise ConnectorError(f"{action} returned an empty SOAP body.")
    return body_el[0]


def _fault_text(fault: ET.Element) -> str:
    """The most useful sentence out of a SOAP fault.

    Microsoft puts the sentence a human needs in an OperationError or
    ApiFaultDetail deep in the detail element; the generic faultstring is
    usually just "Invalid client data".
    """
    messages = []
    for tag in ("Message", "ErrorCode", "faultstring"):
        for el in fault.iter():
            if el.tag.rsplit("}", 1)[-1] == tag and (el.text or "").strip():
                messages.append(el.text.strip())
    if not messages:
        return "".join(fault.itertext()).strip()[:400] or "no detail given"
    # Keep the order but drop repeats.
    seen, out = set(), []
    for m in messages:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return "; ".join(out)[:400]


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _obj(el: ET.Element) -> dict:
    """A SOAP entity as a plain dict, nesting kept.

    A repeated child becomes a list. Its members may be entities or bare
    values -- Microsoft returns arrays of ids and strings as much as arrays
    of objects -- so a member with no children keeps its text rather than
    collapsing to an empty dict.
    """
    out: dict = {}
    for child in el:
        name = _local(child)
        if not len(child):
            out[name] = (child.text or "").strip()
        elif _repeated(child):
            out[name] = [_obj(g) if len(g) else (g.text or "").strip() for g in child]
        else:
            out[name] = _obj(child)
    return out


def _repeated(el: ET.Element) -> bool:
    """True when the element is a list wrapper: every child has the same tag."""
    tags = {_local(c) for c in el}
    return len(el) > 1 and len(tags) == 1


def _collect(parent: ET.Element, wrapper: str) -> list[dict]:
    """The entities inside a named list wrapper, as dicts."""
    node = None
    for el in parent.iter():
        if _local(el) == wrapper:
            node = el
            break
    if node is None:
        return []
    return [_obj(child) for child in node]


# --------------------------------------------------------------------------- #
# entity tools
# --------------------------------------------------------------------------- #
async def list_accounts(conn: Connection, db, args: dict) -> dict:
    """Every ad account under the customer."""
    creds = conn.creds() or {}
    customer = args.get("customer_id") or creds.get("customer_id")
    if not customer:
        raise ConnectorError("No customer_id. Set one on the connection, or pass customer_id.")

    async def _loader():
        body = (
            "<GetAccountsInfoRequest>"
            f"<CustomerId>{_escape(customer)}</CustomerId>"
            "<OnlyParentAccounts>false</OnlyParentAccounts>"
            "</GetAccountsInfoRequest>"
        )
        result = await _soap(
            conn, CUSTOMER_URL, NS_CUSTOMER,
            f"{NS_CUSTOMER}/ICustomerManagementService/GetAccountsInfo",
            body, customer_id=str(customer),
        )
        accounts = _collect(result, "AccountsInfo")
        return {"customer_id": str(customer), "count": len(accounts), "accounts": accounts}

    return await cached("ms_ads", conn.id, "list_accounts", TTL_LONG, _loader,
                        args={"customer_id": str(customer)})


async def get_account(conn: Connection, db, args: dict) -> dict:
    """One ad account in full: name, number, currency, time zone, status."""
    customer, account = _ids(conn, args)

    async def _loader():
        body = (
            "<GetAccountRequest>"
            f"<AccountId>{_escape(account)}</AccountId>"
            "</GetAccountRequest>"
        )
        result = await _soap(
            conn, CUSTOMER_URL, NS_CUSTOMER,
            f"{NS_CUSTOMER}/ICustomerManagementService/GetAccount",
            body, customer_id=customer, account_id=account,
        )
        node = next((el for el in result.iter() if _local(el) == "Account"), None)
        return {"account_id": account, "account": _obj(node) if node is not None else {}}

    return await cached("ms_ads", conn.id, "get_account", TTL_LONG, _loader,
                        args={"account_id": account})


async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    """Every campaign in the account."""
    customer, account = _ids(conn, args)

    async def _loader():
        body = (
            "<GetCampaignsByAccountIdRequest>"
            f"<AccountId>{_escape(account)}</AccountId>"
            "</GetCampaignsByAccountIdRequest>"
        )
        result = await _soap(
            conn, CAMPAIGN_URL, NS_CAMPAIGN,
            f"{NS_CAMPAIGN}/ICampaignManagementService/GetCampaignsByAccountId",
            body, customer_id=customer, account_id=account,
        )
        campaigns = _collect(result, "Campaigns")
        return {"account_id": account, "count": len(campaigns), "campaigns": campaigns}

    return await cached("ms_ads", conn.id, "list_campaigns", TTL_MEDIUM, _loader,
                        args={"account_id": account})


async def list_ad_groups(conn: Connection, db, args: dict) -> dict:
    """Every ad group in one campaign."""
    customer, account = _ids(conn, args)
    campaign = args.get("campaign_id")
    if not campaign:
        raise ConnectorError("campaign_id is required.")

    async def _loader():
        body = (
            "<GetAdGroupsByCampaignIdRequest>"
            f"<CampaignId>{_escape(campaign)}</CampaignId>"
            "</GetAdGroupsByCampaignIdRequest>"
        )
        result = await _soap(
            conn, CAMPAIGN_URL, NS_CAMPAIGN,
            f"{NS_CAMPAIGN}/ICampaignManagementService/GetAdGroupsByCampaignId",
            body, customer_id=customer, account_id=account,
        )
        groups = _collect(result, "AdGroups")
        return {"campaign_id": str(campaign), "count": len(groups), "ad_groups": groups}

    return await cached("ms_ads", conn.id, "list_ad_groups", TTL_MEDIUM, _loader,
                        args={"campaign_id": str(campaign)})


async def list_ads(conn: Connection, db, args: dict) -> dict:
    """Every ad in one ad group."""
    customer, account = _ids(conn, args)
    ad_group = args.get("ad_group_id")
    if not ad_group:
        raise ConnectorError("ad_group_id is required.")

    async def _loader():
        body = (
            "<GetAdsByAdGroupIdRequest>"
            f"<AdGroupId>{_escape(ad_group)}</AdGroupId>"
            "<AdTypes>"
            "<AdType>Text</AdType>"
            "<AdType>ExpandedText</AdType>"
            "<AdType>ResponsiveSearch</AdType>"
            "<AdType>ResponsiveAd</AdType>"
            "<AdType>Product</AdType>"
            "</AdTypes>"
            "</GetAdsByAdGroupIdRequest>"
        )
        result = await _soap(
            conn, CAMPAIGN_URL, NS_CAMPAIGN,
            f"{NS_CAMPAIGN}/ICampaignManagementService/GetAdsByAdGroupId",
            body, customer_id=customer, account_id=account,
        )
        ads = _collect(result, "Ads")
        return {"ad_group_id": str(ad_group), "count": len(ads), "ads": ads}

    return await cached("ms_ads", conn.id, "list_ads", TTL_MEDIUM, _loader,
                        args={"ad_group_id": str(ad_group)})


async def list_keywords(conn: Connection, db, args: dict) -> dict:
    """Every keyword in one ad group, with its match type and bid."""
    customer, account = _ids(conn, args)
    ad_group = args.get("ad_group_id")
    if not ad_group:
        raise ConnectorError("ad_group_id is required.")

    async def _loader():
        body = (
            "<GetKeywordsByAdGroupIdRequest>"
            f"<AdGroupId>{_escape(ad_group)}</AdGroupId>"
            "</GetKeywordsByAdGroupIdRequest>"
        )
        result = await _soap(
            conn, CAMPAIGN_URL, NS_CAMPAIGN,
            f"{NS_CAMPAIGN}/ICampaignManagementService/GetKeywordsByAdGroupId",
            body, customer_id=customer, account_id=account,
        )
        keywords = _collect(result, "Keywords")
        return {"ad_group_id": str(ad_group), "count": len(keywords), "keywords": keywords}

    return await cached("ms_ads", conn.id, "list_keywords", TTL_MEDIUM, _loader,
                        args={"ad_group_id": str(ad_group)})


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _time_xml(args: dict) -> str:
    """<Time> for a report request: a named period, or an explicit range."""
    start, end = args.get("start_date"), args.get("end_date")
    if start and end:
        return (
            "<Time>"
            "<CustomDateRangeStart>"
            f"<Day>{_escape(int(start[8:10]))}</Day>"
            f"<Month>{_escape(int(start[5:7]))}</Month>"
            f"<Year>{_escape(int(start[0:4]))}</Year>"
            "</CustomDateRangeStart>"
            "<CustomDateRangeEnd>"
            f"<Day>{_escape(int(end[8:10]))}</Day>"
            f"<Month>{_escape(int(end[5:7]))}</Month>"
            f"<Year>{_escape(int(end[0:4]))}</Year>"
            "</CustomDateRangeEnd>"
            "</Time>"
        )
    period = str(args.get("date_range") or "last_seven_days").lower()
    if period not in PREDEFINED_TIME:
        raise ConnectorError(
            f"Unknown date_range '{period}'. Use one of: "
            f"{', '.join(sorted(PREDEFINED_TIME))}, or pass start_date and end_date."
        )
    return f"<Time><PredefinedTime>{_TIME_ENUM[period]}</PredefinedTime></Time>"


def _columns_xml(report: str, columns: list[str]) -> str:
    inner = "".join(f"<{report}Column>{_escape(c)}</{report}Column>" for c in columns)
    return f"<Columns>{inner}</Columns>"


async def _report(
    conn: Connection,
    tool: str,
    report: str,
    columns: list[str],
    args: dict,
) -> dict:
    """Submit a report, wait for it, download it, and return its rows.

    ``report`` is Microsoft's request name without the suffix, e.g.
    ``CampaignPerformanceReport``; ``columns`` are that report's column enum
    values.
    """
    customer, account = _ids(conn, args)
    rows_wanted = max(1, min(int(args.get("limit") or 500), 5000))

    scope_xml = (
        "<Scope>"
        "<AccountIds xmlns:a=\"http://schemas.microsoft.com/2003/10/Serialization/Arrays\">"
        f"<a:long>{_escape(account)}</a:long>"
        "</AccountIds>"
        "</Scope>"
    )

    aggregation = str(args.get("aggregation") or "Summary")
    request_xml = (
        "<SubmitGenerateReportRequest>"
        f'<ReportRequest i:type="{report}Request">'
        "<ExcludeColumnHeaders>false</ExcludeColumnHeaders>"
        "<ExcludeReportFooter>true</ExcludeReportFooter>"
        "<ExcludeReportHeader>true</ExcludeReportHeader>"
        "<Format>Csv</Format>"
        "<ReturnOnlyCompleteData>false</ReturnOnlyCompleteData>"
        f"<Aggregation>{_escape(aggregation)}</Aggregation>"
        f"{_columns_xml(report, columns)}"
        f"{scope_xml}"
        f"{_time_xml(args)}"
        "</ReportRequest>"
        "</SubmitGenerateReportRequest>"
    )

    cache_args = {
        "account_id": account,
        "report": report,
        "date_range": args.get("date_range"),
        "start_date": args.get("start_date"),
        "end_date": args.get("end_date"),
        "aggregation": aggregation,
        "limit": rows_wanted,
    }

    async def _loader():
        submitted = await _soap(
            conn, REPORTING_URL, NS_REPORTING,
            f"{NS_REPORTING}/IReportingService/SubmitGenerateReport",
            request_xml, customer_id=customer, account_id=account,
        )
        request_id = next(
            (el.text for el in submitted.iter() if _local(el) == "ReportRequestId"), None
        )
        if not request_id:
            raise ConnectorError(f"{report}: Microsoft did not return a report request id.")

        url = await _poll(conn, customer, account, request_id, report)
        rows = await _download(url, rows_wanted, report)
        return {
            "account_id": account,
            "report": report,
            "aggregation": aggregation,
            "row_count": len(rows),
            "truncated": len(rows) >= rows_wanted,
            "rows": rows,
        }

    return await cached("ms_ads", conn.id, tool, TTL_MEDIUM, _loader, args=cache_args)


async def _poll(conn: Connection, customer: str, account: str, request_id: str, report: str) -> str:
    """Wait for a submitted report and return its download URL."""
    deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
    while True:
        body = (
            "<PollGenerateReportRequest>"
            f"<ReportRequestId>{_escape(request_id)}</ReportRequestId>"
            "</PollGenerateReportRequest>"
        )
        result = await _soap(
            conn, REPORTING_URL, NS_REPORTING,
            f"{NS_REPORTING}/IReportingService/PollGenerateReport",
            body, customer_id=customer, account_id=account,
        )
        status = next((el.text for el in result.iter() if _local(el) == "Status"), "")
        if status == "Success":
            url = next(
                (el.text for el in result.iter() if _local(el) == "ReportDownloadUrl"), None
            )
            if not url:
                # Success with no URL means the report matched no rows at all.
                return ""
            return url
        if status == "Error":
            raise ConnectorError(
                f"{report}: Microsoft could not generate the report. "
                "Check that the account has data for the period requested."
            )
        if time.monotonic() >= deadline:
            raise ConnectorError(
                f"{report}: the report was still generating after "
                f"{REPORT_TIMEOUT_SECONDS}s. Ask for a shorter period."
            )
        await asyncio.sleep(REPORT_POLL_SECONDS)


async def _download(url: str, limit: int, report: str) -> list[dict]:
    """Fetch the report ZIP and read its single CSV member."""
    if not url:
        return []
    try:
        async with limit_for(url):
            res = await http_get(url)
    except UpstreamUnavailable as exc:
        raise ConnectorError(str(exc))
    if res.status_code >= 400:
        raise ConnectorError(f"{report}: downloading the report failed {res.status_code}.")
    return _rows_from_zip(res.content, limit, report)


def _rows_from_zip(payload: bytes, limit: int, report: str) -> list[dict]:
    """The CSV rows inside a Microsoft report ZIP.

    Kept separate from the download so it can be tested without the network.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise ConnectorError(f"{report}: the downloaded report was not a ZIP file.")
    names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise ConnectorError(f"{report}: the downloaded report held no CSV.")
    with archive.open(names[0]) as handle:
        text = handle.read().decode("utf-8-sig", errors="replace")
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        # Microsoft ends the CSV with a blank line and, on some reports, a
        # copyright line that DictReader hands back with a None key.
        if None in row or not any((v or "").strip() for v in row.values()):
            continue
        rows.append({k: v for k, v in row.items() if k})
        if len(rows) >= limit:
            break
    return rows


# --------------------------------------------------------------------------- #
# report tools
# --------------------------------------------------------------------------- #
_SPEND = ["Impressions", "Clicks", "Spend", "Ctr", "AverageCpc", "Conversions", "Revenue"]


async def account_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results for the whole account."""
    return await _report(
        conn, "account_performance", "AccountPerformanceReport",
        ["AccountName", "AccountId", "TimePeriod", *_SPEND], args,
    )


async def campaign_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results per campaign."""
    return await _report(
        conn, "campaign_performance", "CampaignPerformanceReport",
        ["CampaignName", "CampaignId", "CampaignStatus", "TimePeriod", *_SPEND], args,
    )


async def ad_group_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results per ad group."""
    return await _report(
        conn, "ad_group_performance", "AdGroupPerformanceReport",
        ["CampaignName", "AdGroupName", "AdGroupId", "Status", "TimePeriod", *_SPEND], args,
    )


async def ad_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results per ad."""
    return await _report(
        conn, "ad_performance", "AdPerformanceReport",
        ["CampaignName", "AdGroupName", "AdId", "AdTitle", "TimePeriod", *_SPEND], args,
    )


async def keyword_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results per keyword, with match type and quality score."""
    return await _report(
        conn, "keyword_performance", "KeywordPerformanceReport",
        ["CampaignName", "AdGroupName", "Keyword", "KeywordId", "BidMatchType",
         "QualityScore", "TimePeriod", *_SPEND], args,
    )


async def search_query_performance(conn: Connection, db, args: dict) -> dict:
    """What people actually typed, and what it cost."""
    return await _report(
        conn, "search_query_performance", "SearchQueryPerformanceReport",
        ["CampaignName", "AdGroupName", "SearchQuery", "Keyword", "MatchType",
         "TimePeriod", *_SPEND], args,
    )


async def geographic_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results by country, region and city."""
    return await _report(
        conn, "geographic_performance", "GeographicPerformanceReport",
        ["CampaignName", "Country", "State", "City", "TimePeriod", *_SPEND], args,
    )


async def device_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results split by device."""
    return await _report(
        conn, "device_performance", "CampaignPerformanceReport",
        ["CampaignName", "DeviceType", "TimePeriod", *_SPEND], args,
    )


async def age_gender_performance(conn: Connection, db, args: dict) -> dict:
    """Spend and results by age group and gender."""
    return await _report(
        conn, "age_gender_performance", "AgeGenderAudienceReport",
        ["CampaignName", "AdGroupName", "AgeGroup", "Gender", "TimePeriod",
         "Impressions", "Clicks", "Conversions", "Spend"], args,
    )


async def budget_summary(conn: Connection, db, args: dict) -> dict:
    """What each campaign's budget was and how much of it went."""
    return await _report(
        conn, "budget_summary", "BudgetSummaryReport",
        ["CampaignName", "CampaignId", "Date", "MonthlyBudget",
         "DailySpend", "MonthToDateSpend"], args,
    )


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
_ACCOUNT_PROPS = {
    "customer_id": {
        "type": "string",
        "description": "Microsoft Advertising customer id (defaults to the connection's).",
    },
    "account_id": {
        "type": "string",
        "description": "Ad account id (defaults to the connection's).",
    },
}

_REPORT_PROPS = {
    **_ACCOUNT_PROPS,
    "date_range": {
        "type": "string",
        "enum": sorted(PREDEFINED_TIME),
        "description": "A named period. Ignored when start_date and end_date are given. Default last_seven_days.",
    },
    "start_date": {"type": "string", "description": "Start of a custom range, YYYY-MM-DD."},
    "end_date": {"type": "string", "description": "End of a custom range, YYYY-MM-DD."},
    "aggregation": {
        "type": "string",
        "enum": ["Summary", "Hourly", "Daily", "Weekly", "Monthly", "Yearly"],
        "description": "How rows are grouped over time. Default Summary (one row per entity).",
    },
    "limit": {
        "type": "integer",
        "description": "Most rows to return, 1-5000. Default 500.",
    },
}


def _report_entry(description: str) -> dict:
    return {
        "description": description,
        "input": {
            "type": "object",
            "properties": dict(_REPORT_PROPS),
            "required": [],
            "additionalProperties": False,
        },
    }


CATALOG: dict[str, dict] = {
    "list_accounts": {
        "description": "Every Microsoft Advertising ad account under the customer, with id, name and number.",
        "input": {
            "type": "object",
            "properties": {"customer_id": _ACCOUNT_PROPS["customer_id"]},
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_account": {
        "description": "One ad account in full: name, account number, currency, time zone and status.",
        "input": {
            "type": "object",
            "properties": dict(_ACCOUNT_PROPS),
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_campaigns": {
        "description": "Every campaign in the account, with budget, status and type.",
        "input": {
            "type": "object",
            "properties": dict(_ACCOUNT_PROPS),
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_ad_groups": {
        "description": "Every ad group in one campaign.",
        "input": {
            "type": "object",
            "properties": {
                **_ACCOUNT_PROPS,
                "campaign_id": {"type": "string", "description": "The campaign to list."},
            },
            "required": ["campaign_id"],
            "additionalProperties": False,
        },
    },
    "list_ads": {
        "description": "Every ad in one ad group, across the common ad types.",
        "input": {
            "type": "object",
            "properties": {
                **_ACCOUNT_PROPS,
                "ad_group_id": {"type": "string", "description": "The ad group to list."},
            },
            "required": ["ad_group_id"],
            "additionalProperties": False,
        },
    },
    "list_keywords": {
        "description": "Every keyword in one ad group, with match type, bid and status.",
        "input": {
            "type": "object",
            "properties": {
                **_ACCOUNT_PROPS,
                "ad_group_id": {"type": "string", "description": "The ad group to list."},
            },
            "required": ["ad_group_id"],
            "additionalProperties": False,
        },
    },
    "account_performance": _report_entry(
        "Impressions, clicks, spend, CTR, average CPC, conversions and revenue for the whole account."
    ),
    "campaign_performance": _report_entry(
        "The same figures per campaign, with each campaign's status."
    ),
    "ad_group_performance": _report_entry("The same figures per ad group."),
    "ad_performance": _report_entry("The same figures per ad, with the ad's title."),
    "keyword_performance": _report_entry(
        "The same figures per keyword, with match type and quality score."
    ),
    "search_query_performance": _report_entry(
        "The search terms people actually typed, the keyword each matched, and what they cost."
    ),
    "geographic_performance": _report_entry(
        "Spend and results by country, region and city."
    ),
    "device_performance": _report_entry(
        "Spend and results split by device type."
    ),
    "age_gender_performance": _report_entry(
        "Spend and results by age group and gender."
    ),
    "budget_summary": _report_entry(
        "Each campaign's monthly budget against its daily and month-to-date spend."
    ),
}

HANDLERS = {
    "list_accounts": list_accounts,
    "get_account": get_account,
    "list_campaigns": list_campaigns,
    "list_ad_groups": list_ad_groups,
    "list_ads": list_ads,
    "list_keywords": list_keywords,
    "account_performance": account_performance,
    "campaign_performance": campaign_performance,
    "ad_group_performance": ad_group_performance,
    "ad_performance": ad_performance,
    "keyword_performance": keyword_performance,
    "search_query_performance": search_query_performance,
    "geographic_performance": geographic_performance,
    "device_performance": device_performance,
    "age_gender_performance": age_gender_performance,
    "budget_summary": budget_summary,
}

registry.register(
    Connector(
        slug="ms_ads",
        label="Microsoft Advertising",
        auth="api_key",
        cred_fields=["developer_token", "client_id", "client_secret", "refresh_token"],
        setup_fields=["customer_id", "account_id"],
        description=(
            "Reads Microsoft Advertising (Bing Ads) accounts, campaigns, ad groups, ads and "
            "keywords, and pulls performance reports - spend, clicks, conversions, search "
            "terms, geography, device and demographics. Read-only."
        ),
        category="Advertising",
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
