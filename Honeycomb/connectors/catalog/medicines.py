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

Evidence, for content that has to cite something (all keyless):
  search_clinical_trials / get_clinical_trial : ClinicalTrials.gov API v2,
      which also registers most trials run in India
  search_pubmed / get_pubmed_abstracts        : NCBI E-utilities (PubMed)
  dailymed_labels                             : NLM DailyMed label versions

India. There is no public API for Indian drug regulation, so these search
datasets bundled in connectors/data/india/, extracted from the regulators'
own PDFs by build_cdsco.py there:
  india_approved_drugs     : CDSCO "new drugs approved" lists, 1961-2026
  india_essential_medicines: National List of Essential Medicines 2022
  india_ceiling_prices     : NPPA ceiling prices under DPCO 2013 -- as of
                             30.09.2020, the latest consolidated list NPPA
                             publishes; every answer says so
  india_drug_profile       : all three for one drug in one call
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

FDA_BASE = "https://api.fda.gov"
RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"
CTGOV_BASE = "https://clinicaltrials.gov/api/v2"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"

INDIA_DATA = Path(__file__).resolve().parent.parent / "data" / "india"

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
# Evidence: ClinicalTrials.gov, PubMed, DailyMed
# ============================================================

TRIAL_STATUSES = (
    "RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED",
    "TERMINATED", "WITHDRAWN", "SUSPENDED", "ENROLLING_BY_INVITATION", "UNKNOWN",
)
TRIAL_PHASES = ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA")


def _trial_row(study: dict) -> dict:
    ps = study.get("protocolSection") or {}
    ident = ps.get("identificationModule") or {}
    status = ps.get("statusModule") or {}
    design = ps.get("designModule") or {}
    nct = ident.get("nctId")
    countries = sorted({
        loc.get("country") for loc in (ps.get("contactsLocationsModule") or {}).get("locations", []) or []
        if loc.get("country")
    })
    return {
        "nct_id": nct,
        "title": ident.get("briefTitle"),
        "status": status.get("overallStatus"),
        "phases": design.get("phases") or [],
        "start_date": (status.get("startDateStruct") or {}).get("date"),
        "completion_date": (status.get("completionDateStruct") or {}).get("date"),
        "conditions": (ps.get("conditionsModule") or {}).get("conditions") or [],
        "interventions": [i.get("name") for i in (ps.get("armsInterventionsModule") or {}).get("interventions", []) or []],
        "sponsor": ((ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name"),
        "countries": countries,
        "has_results": bool(study.get("hasResults")),
        "url": f"https://clinicaltrials.gov/study/{nct}" if nct else None,
    }


async def search_clinical_trials(conn: Connection, db, args: dict) -> dict:
    """Registered clinical trials, filterable by drug, condition, country, status and phase."""
    params: dict = {"pageSize": _limit(args, 10, 50), "countTotal": "true", "format": "json"}
    for arg, key in (("query", "query.term"), ("condition", "query.cond"),
                     ("intervention", "query.intr"), ("location", "query.locn"),
                     ("sponsor", "query.spons")):
        value = str(args.get(arg) or "").strip()
        if value:
            params[key] = value
    if not any(k.startswith("query.") for k in params):
        raise ConnectorError("Give at least one of query, condition, intervention, location or sponsor.")
    statuses = [s.upper() for s in (args.get("status") or []) if s]
    bad = [s for s in statuses if s not in TRIAL_STATUSES]
    if bad:
        raise ConnectorError(f"Unknown status {bad}; use {', '.join(TRIAL_STATUSES)}.")
    if statuses:
        params["filter.overallStatus"] = ",".join(statuses)
    phase = str(args.get("phase") or "").upper()
    if phase:
        if phase not in TRIAL_PHASES:
            raise ConnectorError(f"phase must be one of {', '.join(TRIAL_PHASES)}.")
        params["filter.advanced"] = f"AREA[Phase]{phase}"
    if args.get("page_token"):
        params["pageToken"] = str(args["page_token"])
    sort = args.get("sort")
    if sort == "newest":
        params["sort"] = "StartDate:desc"

    async def _loader():
        data = await _get_json(f"{CTGOV_BASE}/studies", params)
        return {
            "total_count": data.get("totalCount"),
            "trials": [_trial_row(s) for s in data.get("studies") or []],
            "next_page_token": data.get("nextPageToken"),
            "source": "ClinicalTrials.gov",
        }

    return await cached("medicines", conn.id, "search_clinical_trials", TTL_MEDIUM, _loader, args=params)


async def get_clinical_trial(conn: Connection, db, args: dict) -> dict:
    """One trial in detail: summary, design, enrolment, eligibility, outcomes and sites."""
    nct = str(args.get("nct_id") or "").strip().upper()
    if not re.fullmatch(r"NCT\d{8}", nct):
        raise ConnectorError("nct_id must look like NCT01234567.")

    async def _loader():
        data = await _get_json(f"{CTGOV_BASE}/studies/{nct}", {"format": "json"})
        if not data or not data.get("protocolSection"):
            return {"nct_id": nct, "found": False}
        ps = data["protocolSection"]
        row = _trial_row(data)
        design = ps.get("designModule") or {}
        locations = (ps.get("contactsLocationsModule") or {}).get("locations", []) or []
        row.update({
            "found": True,
            "official_title": (ps.get("identificationModule") or {}).get("officialTitle"),
            "brief_summary": (ps.get("descriptionModule") or {}).get("briefSummary"),
            "study_type": design.get("studyType"),
            "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
            "primary_outcomes": [o.get("measure") for o in (ps.get("outcomesModule") or {}).get("primaryOutcomes", []) or []],
            "eligibility": (ps.get("eligibilityModule") or {}).get("eligibilityCriteria"),
            "sites_in_india": [
                {"facility": loc.get("facility"), "city": loc.get("city")}
                for loc in locations if loc.get("country") == "India"
            ][:50],
            "site_count": len(locations),
        })
        return row

    return await cached("medicines", conn.id, "get_clinical_trial", TTL_MEDIUM, _loader, args={"nct": nct})


_NCBI = {"tool": "honeycomb", "retmode": "json"}


async def search_pubmed(conn: Connection, db, args: dict) -> dict:
    """PubMed citations for a query, with journal, date, authors and DOI."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ConnectorError("query is required, e.g. 'semaglutide India'.")
    limit = _limit(args, 10, 50)
    params = {**_NCBI, "db": "pubmed", "term": query, "retmax": limit,
              "sort": "pub_date" if args.get("sort") == "newest" else "relevance"}
    if args.get("from_year") or args.get("to_year"):
        params.update({"datetype": "pdat", "mindate": str(args.get("from_year") or 1800),
                       "maxdate": str(args.get("to_year") or 3000)})

    async def _loader():
        found = (await _get_json(f"{EUTILS_BASE}/esearch.fcgi", params)).get("esearchresult") or {}
        ids = found.get("idlist") or []
        rows = []
        if ids:
            summary = (await _get_json(f"{EUTILS_BASE}/esummary.fcgi",
                                       {**_NCBI, "db": "pubmed", "id": ",".join(ids)})).get("result") or {}
            for pmid in ids:
                item = summary.get(pmid) or {}
                doi = next((a.get("value") for a in item.get("articleids", []) if a.get("idtype") == "doi"), None)
                rows.append({
                    "pmid": pmid,
                    "title": item.get("title"),
                    "journal": item.get("fulljournalname") or item.get("source"),
                    "published": item.get("pubdate"),
                    "authors": [a.get("name") for a in item.get("authors", [])[:3]],
                    "publication_types": item.get("pubtype", []),
                    "doi": doi,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
        return {"query": query, "total_count": int(found.get("count") or 0), "articles": rows,
                "source": "PubMed (NCBI E-utilities)"}

    return await cached("medicines", conn.id, "search_pubmed", TTL_MEDIUM, _loader, args=params)


async def get_pubmed_abstracts(conn: Connection, db, args: dict) -> dict:
    """Full abstracts for up to 20 PubMed ids."""
    raw = args.get("pmids") or []
    pmids = [str(p).strip() for p in (raw if isinstance(raw, list) else str(raw).split(","))]
    pmids = [p for p in pmids if p.isdigit()][:20]
    if not pmids:
        raise ConnectorError("pmids is required: a list of PubMed ids, e.g. ['37622681'].")

    async def _loader():
        url = f"{EUTILS_BASE}/efetch.fcgi"
        try:
            async with limit_for(url):
                res = await http_get(url, headers={"User-Agent": _HEADERS["User-Agent"]},
                                     params={"tool": "honeycomb", "db": "pubmed",
                                             "id": ",".join(pmids), "retmode": "xml"})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
        if res.status_code >= 400:
            raise ConnectorError(f"PubMed error {res.status_code}.")
        try:
            root = ET.fromstring(res.content)
        except ET.ParseError:
            raise ConnectorError("PubMed returned unreadable XML.")
        out = []
        for art in root.findall(".//PubmedArticle"):
            text = lambda path: "".join(art.find(path).itertext()).strip() if art.find(path) is not None else None
            parts = []
            for node in art.findall(".//Abstract/AbstractText"):
                label = node.get("Label")
                body = "".join(node.itertext()).strip()
                parts.append(f"{label}: {body}" if label else body)
            out.append({
                "pmid": text(".//PMID"),
                "title": text(".//ArticleTitle"),
                "journal": text(".//Journal/Title"),
                "year": text(".//JournalIssue/PubDate/Year") or text(".//JournalIssue/PubDate/MedlineDate"),
                "abstract": "\n".join(parts) or None,
                "doi": next(("".join(i.itertext()) for i in art.findall(".//ArticleId")
                             if i.get("IdType") == "doi"), None),
                "mesh_terms": ["".join(m.itertext()) for m in art.findall(".//MeshHeading/DescriptorName")],
            })
        return {"count": len(out), "articles": out, "source": "PubMed (NCBI E-utilities)"}

    return await cached("medicines", conn.id, "get_pubmed_abstracts", TTL_LONG, _loader, args={"ids": pmids})


async def dailymed_labels(conn: Connection, db, args: dict) -> dict:
    """Current US product labels (SPL) on DailyMed for a drug name, with links."""
    name = _name(args)
    limit = _limit(args, 10, 50)

    async def _loader():
        data = await _get_json(f"{DAILYMED_BASE}/spls.json", {"drug_name": name, "pagesize": limit})
        rows = [{
            "title": r.get("title"),
            "published": r.get("published_date"),
            "version": r.get("spl_version"),
            "setid": r.get("setid"),
            "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={r.get('setid')}",
            "pdf": f"https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setId={r.get('setid')}",
        } for r in data.get("data") or []]
        return {"name": name, "total_count": (data.get("metadata") or {}).get("total_elements"),
                "labels": rows, "source": "DailyMed (US National Library of Medicine)"}

    return await cached("medicines", conn.id, "dailymed_labels", TTL_LONG, _loader, args={"name": name, "limit": limit})


# ============================================================
# India: bundled CDSCO / NLEM / NPPA data
# ============================================================

@lru_cache(maxsize=None)
def _india(name: str) -> dict:
    """One bundled dataset, read once per process."""
    try:
        return json.loads((INDIA_DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConnectorError(f"The bundled India dataset {name} is missing or unreadable.")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _matches(query: str, *fields: str) -> bool:
    wanted = _tokens(query)
    haystack = " ".join(_tokens(" ".join(fields)))
    return bool(wanted) and all(t in haystack for t in wanted)


def _query(args: dict) -> str:
    q = str(args.get("query") or args.get("name") or "").strip()
    if not q:
        raise ConnectorError("query is required: a drug name (or, where offered, an indication).")
    return q


def _year(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _india_approvals(query: str, in_indication: bool, year_from, year_to, limit: int) -> dict:
    data = _india("cdsco_new_drugs.json")
    lo, hi = _year(year_from), _year(year_to)
    hits = []
    for row in data["rows"]:
        fields = (row["drug"], row["indication"]) if in_indication else (row["drug"],)
        if not _matches(query, *fields):
            continue
        year = _year((row.get("date") or "")[:4])
        if (lo and (not year or year < lo)) or (hi and (not year or year > hi)):
            continue
        hits.append(row)
    hits.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {
        "match_count": len(hits),
        "approvals": [{**r, "source_pdf": data["lists"].get(r["list"])} for r in hits[:limit]],
        "source": data["source"],
        "coverage_note": data["note"],
    }


async def india_approved_drugs(conn: Connection, db, args: dict) -> dict:
    """CDSCO new-drug approvals in India: what, for which indication, and when."""
    return _india_approvals(_query(args), bool(args.get("search_indications")),
                            args.get("year_from"), args.get("year_to"), _limit(args, 20, 200))


def _india_nlem(query: str, limit: int) -> dict:
    data = _india("nlem_2022.json")
    hits = [r for r in data["rows"] if _matches(query, r["medicine"])]
    return {"on_nlem_2022": bool(hits), "entries": hits[:limit], "source": data["source"],
            "source_pdf": data["url"],
            "note": "Dosage forms are extracted from the gazette PDF; quote them from the source."}


async def india_essential_medicines(conn: Connection, db, args: dict) -> dict:
    """Whether a medicine is on India's National List of Essential Medicines 2022."""
    return _india_nlem(_query(args), _limit(args, 20, 100))


def _india_prices(query: str, limit: int) -> dict:
    data = _india("nppa_ceiling_prices.json")
    hits = [r for r in data["rows"] if _matches(query, r["medicine"])]
    return {
        "match_count": len(hits),
        "prices": hits[:limit],
        "as_of": data["as_of"],
        "source": data["source"],
        "source_pdf": data["url"],
        "note": ("Ceiling prices (INR, excluding GST) as of {0}. NPPA revises them every April "
                 "with the wholesale price index, so check the current NPPA notification before "
                 "publishing a price.").format(data["as_of"]),
    }


async def india_ceiling_prices(conn: Connection, db, args: dict) -> dict:
    """NPPA ceiling prices for scheduled formulations (price-controlled medicines)."""
    return _india_prices(_query(args), _limit(args, 20, 100))


async def india_drug_profile(conn: Connection, db, args: dict) -> dict:
    """Everything the bundled India data says about one drug, in one call."""
    query = _query(args)
    approvals = _india_approvals(query, False, None, None, 10)
    nlem = _india_nlem(query, 10)
    prices = _india_prices(query, 20)
    return {
        "query": query,
        "cdsco_approvals": approvals,
        "essential_medicine": nlem,
        "price_control": {**prices, "price_controlled": bool(prices["match_count"])},
        "tip": "For US label text use drug_label; for evidence use search_pubmed and "
               "search_clinical_trials with location='India'.",
    }

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
    "search_clinical_trials": {
        "description": "Search registered clinical trials (ClinicalTrials.gov) by drug, condition, sponsor or country -- e.g. location='India' -- with status and phase filters. Returns NCT id, phase, status, dates, sponsor, countries and a link.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text, e.g. 'semaglutide obesity'."},
                "condition": {"type": "string", "description": "Condition or disease."},
                "intervention": {"type": "string", "description": "Drug or intervention name."},
                "location": {"type": "string", "description": "Country or city, e.g. 'India' or 'Mumbai'."},
                "sponsor": {"type": "string", "description": "Lead sponsor, e.g. 'Sun Pharma'."},
                "status": {"type": "array", "items": {"type": "string", "enum": list(TRIAL_STATUSES)}},
                "phase": {"type": "string", "enum": list(TRIAL_PHASES)},
                "sort": {"type": "string", "enum": ["relevance", "newest"]},
                "limit": {"type": "integer", "description": "1-50. Default 10."},
                "page_token": {"type": "string", "description": "next_page_token from a previous call."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_clinical_trial": {
        "description": "One clinical trial in detail: summary, design, enrolment, eligibility, primary outcomes, and its sites in India.",
        "input": {"type": "object", "properties": {"nct_id": {"type": "string", "description": "e.g. NCT05352815"}},
                  "required": ["nct_id"], "additionalProperties": False},
    },
    "search_pubmed": {
        "description": "Search PubMed for studies to cite: title, journal, date, first authors, publication type, DOI and link.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PubMed query, e.g. 'metformin India randomized'."},
                "from_year": {"type": "integer"},
                "to_year": {"type": "integer"},
                "sort": {"type": "string", "enum": ["relevance", "newest"]},
                "limit": {"type": "integer", "description": "1-50. Default 10."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "get_pubmed_abstracts": {
        "description": "Full abstracts, DOI and MeSH terms for up to 20 PubMed ids (from search_pubmed).",
        "input": {"type": "object", "properties": {"pmids": {"type": "array", "items": {"type": "string"}}},
                  "required": ["pmids"], "additionalProperties": False},
    },
    "dailymed_labels": {
        "description": "Current US product labels for a drug on DailyMed, with version, date and links to the label page and PDF.",
        "input": {"type": "object", "properties": dict(_NAME_LIMIT), "required": ["name"], "additionalProperties": False},
    },
    "india_approved_drugs": {
        "description": "India: CDSCO new-drug approvals 1961-2026 -- drug and strength, approved indication, approval date, and the source PDF. Search by drug name, or by indication with search_indications.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Drug name, or an indication when search_indications is true."},
                "search_indications": {"type": "boolean", "description": "Also match the indication text."},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
                "limit": {"type": "integer", "description": "1-200. Default 20."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "india_essential_medicines": {
        "description": "India: whether a medicine is on the National List of Essential Medicines 2022, with its section, level of care and dosage forms.",
        "input": {"type": "object", "properties": {"query": {"type": "string", "description": "Medicine name."},
                                                   "limit": {"type": "integer"}},
                  "required": ["query"], "additionalProperties": False},
    },
    "india_ceiling_prices": {
        "description": "India: NPPA ceiling prices (INR per unit) for price-controlled formulations under DPCO 2013. Data as of 30 Sep 2020 -- the answer says so; confirm current prices with NPPA.",
        "input": {"type": "object", "properties": {"query": {"type": "string", "description": "Medicine name."},
                                                   "limit": {"type": "integer"}},
                  "required": ["query"], "additionalProperties": False},
    },
    "india_drug_profile": {
        "description": "India in one call: CDSCO approvals, NLEM 2022 status and NPPA price control for a drug.",
        "input": {"type": "object", "properties": {"query": {"type": "string", "description": "Drug name."}},
                  "required": ["query"], "additionalProperties": False},
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
    "search_clinical_trials": search_clinical_trials,
    "get_clinical_trial": get_clinical_trial,
    "search_pubmed": search_pubmed,
    "get_pubmed_abstracts": get_pubmed_abstracts,
    "dailymed_labels": dailymed_labels,
    "india_approved_drugs": india_approved_drugs,
    "india_essential_medicines": india_essential_medicines,
    "india_ceiling_prices": india_ceiling_prices,
    "india_drug_profile": india_drug_profile,
}

registry.register(
    Connector(
        slug="medicines",
        label="Medicines & Drug Data",
        auth="api_key",
        cred_fields=[],
        catalog=CATALOG,
        handlers=HANDLERS,
        description=('Drug facts and evidence for pharma content: FDA labels, adverse events and recalls, '
                     'DailyMed, RxNorm names, ClinicalTrials.gov and PubMed, plus India -- CDSCO approvals, '
                     'the National List of Essential Medicines 2022 and NPPA ceiling prices. No API key.'),
        category='Reference',
    )
)
