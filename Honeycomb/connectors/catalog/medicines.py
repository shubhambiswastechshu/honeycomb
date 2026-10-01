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

Regulatory and reference, the rest of the world (all keyless):
  fda_approval_history : Drugs@FDA applications, products and approval dates
  orange_book_patents  : patents and exclusivities per FDA application
  ema_medicines        : EU centrally authorised medicines (EMA's daily file)
  drug_shortages       : FDA drug shortage list
  atc_classification   : WHO ATC classes, via NIH RxClass
  patient_drug_info    : MedlinePlus plain-language drug pages
  europe_pmc_search / europe_pmc_full_text : open-access papers, full text
"""
import html
import json
import re
import time
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
ORANGE_BOOK = "https://www.accessdata.fda.gov/scripts/cder/ob"
EMA_JSON = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
MEDLINEPLUS_CONNECT = "https://connect.medlineplus.gov/service"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

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
# Regulatory & reference: FDA, EMA, WHO ATC, MedlinePlus, Europe PMC
# ============================================================

def _fda_date(value: str | None) -> str | None:
    """openFDA's YYYYMMDD as YYYY-MM-DD."""
    v = str(value or "")
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else (v or None)


def _plain(text: str | None, limit: int = 1200) -> str | None:
    if not text:
        return None
    flat = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


async def _drugsfda(name: str, limit: int) -> list[dict]:
    data = await _get_json(
        f"{FDA_BASE}/drug/drugsfda.json",
        {"search": _or_search("openfda.brand_name", "openfda.generic_name", name), "limit": limit},
    )
    return data.get("results") or []


async def fda_approval_history(conn: Connection, db, args: dict) -> dict:
    """Drugs@FDA: who holds each US application, its products, and when it was approved."""
    name = _name(args)
    limit = _limit(args, 5, 20)

    async def _loader():
        apps = []
        for app in await _drugsfda(name, limit):
            subs = app.get("submissions") or []
            original = next((x for x in subs if x.get("submission_type") == "ORIG"
                             and x.get("submission_status") == "AP"), None)
            recent = sorted(subs, key=lambda x: x.get("submission_status_date") or "", reverse=True)[:8]
            apps.append({
                "application_number": app.get("application_number"),
                "sponsor": app.get("sponsor_name"),
                "original_approval": _fda_date((original or {}).get("submission_status_date")),
                "products": [{
                    "product_number": pr.get("product_number"),
                    "brand_name": pr.get("brand_name"),
                    "active_ingredients": pr.get("active_ingredients"),
                    "dosage_form": pr.get("dosage_form"),
                    "route": pr.get("route"),
                    "marketing_status": pr.get("marketing_status"),
                    "te_code": pr.get("te_code"),
                } for pr in app.get("products") or []],
                "recent_actions": [{
                    "type": x.get("submission_type"),
                    "class": x.get("submission_class_code_description"),
                    "status": x.get("submission_status"),
                    "date": _fda_date(x.get("submission_status_date")),
                    "documents": [{"type": d.get("type"), "url": d.get("url")}
                                  for d in x.get("application_docs") or []],
                } for x in recent],
            })
        return {"name": name, "found": bool(apps), "applications": apps, "source": "Drugs@FDA (openFDA)"}

    return await cached("medicines", conn.id, "fda_approval_history", TTL_LONG, _loader,
                        args={"name": name, "limit": limit})


def _html_tables(page: str) -> list[list[list[str]]]:
    tables = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S | re.I):
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
            cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


async def _orange_book_product(app_type: str, app_no: str, product_no: str) -> dict:
    url = f"{ORANGE_BOOK}/patent_info.cfm"
    params = {"Product_No": product_no, "Appl_No": app_no, "Appl_type": app_type}
    try:
        async with limit_for(url):
            res = await http_get(url, headers={"User-Agent": "Mozilla/5.0"}, params=params)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(f"Orange Book returned {res.status_code}.")
    patents, exclusivities = [], []
    for rows in _html_tables(res.text):
        header = [h.lower() for h in rows[0]]
        records = [dict(zip(rows[0], r)) for r in rows[1:] if len(r) == len(rows[0])]
        if "patent no" in header:
            patents += [{
                "patent": r.get("Patent No"),
                "expires": r.get("Patent Expiration"),
                "drug_substance": r.get("Drug Substance") == "DS",
                "drug_product": r.get("Drug Product") == "DP",
                "use_code": r.get("Patent Use Code") or None,
                "delist_requested": r.get("Delist Requested") == "Y",
            } for r in records]
        elif "exclusivity code" in header:
            exclusivities += [{"code": r.get("Exclusivity Code"), "expires": r.get("Exclusivity Expiration")}
                              for r in records]
    return {"patents": patents, "exclusivities": exclusivities}


def _us_date(value: str | None) -> str:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", value or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


async def orange_book_patents(conn: Connection, db, args: dict) -> dict:
    """Patents and regulatory exclusivities listed in the FDA Orange Book for a drug."""
    name = _name(args)

    async def _loader():
        out = []
        for app in await _drugsfda(name, 3):
            number = str(app.get("application_number") or "")
            m = re.match(r"^(NDA|ANDA|BLA)(\d+)$", number)
            if not m or m.group(1) == "BLA":
                continue  # biologics are in the Purple Book, not the Orange Book
            app_type = "N" if m.group(1) == "NDA" else "A"
            for product in (app.get("products") or [])[:4]:
                listing = await _orange_book_product(app_type, m.group(2), product.get("product_number") or "001")
                out.append({
                    "application_number": number,
                    "sponsor": app.get("sponsor_name"),
                    "product_number": product.get("product_number"),
                    "brand_name": product.get("brand_name"),
                    "strength": ", ".join(f"{i.get('name')} {i.get('strength')}" for i in product.get("active_ingredients") or []),
                    **listing,
                })
        expiries = sorted(_us_date(p["expires"]) for row in out for p in row["patents"] if _us_date(p["expires"]))
        return {
            "name": name,
            "found": bool(out),
            "latest_patent_expiry": expiries[-1] if expiries else None,
            "products": out,
            "source": "FDA Orange Book",
            "note": "Listed patents and exclusivities only -- generic entry timing is a legal question, "
                    "not something this list settles. Biologics are in the FDA Purple Book instead.",
        }

    return await cached("medicines", conn.id, "orange_book_patents", TTL_LONG, _loader, args={"name": name})


_EMA_CACHE: dict = {"at": 0.0, "rows": None}
_EMA_TTL = 24 * 60 * 60


async def _ema_rows() -> list[dict]:
    """EMA's full medicines file (~7 MB, refreshed daily), held per process for a day."""
    if _EMA_CACHE["rows"] is not None and time.time() - _EMA_CACHE["at"] < _EMA_TTL:
        return _EMA_CACHE["rows"]
    try:
        async with limit_for(EMA_JSON):
            res = await http_get(EMA_JSON, headers={"User-Agent": _HEADERS["User-Agent"]}, timeout=60)
    except UpstreamUnavailable as e:
        if _EMA_CACHE["rows"] is not None:
            return _EMA_CACHE["rows"]  # yesterday's copy beats no answer
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(f"EMA returned {res.status_code}.")
    try:
        rows = res.json().get("data") or []
    except ValueError:
        raise ConnectorError("EMA returned an unreadable medicines file.")
    _EMA_CACHE.update(at=time.time(), rows=rows)
    return rows


def _eu_date(value: str | None) -> str | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", value or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


async def ema_medicines(conn: Connection, db, args: dict) -> dict:
    """EU centrally authorised medicines: status, holder, dates, indication and EMA flags."""
    query = _query(args)
    limit = _limit(args, 10, 50)
    in_indication = bool(args.get("search_indications"))
    status = str(args.get("status") or "").strip().lower()
    hits = []
    for r in await _ema_rows():
        if r.get("category") != "Human":
            continue
        fields = [r.get("name_of_medicine") or "", r.get("active_substance") or "",
                  r.get("international_non_proprietary_name_common_name") or ""]
        if in_indication:
            fields += [r.get("therapeutic_indication") or "", r.get("therapeutic_area_mesh") or ""]
        if not _matches(query, *fields):
            continue
        if status and status not in (r.get("medicine_status") or "").lower():
            continue
        hits.append(r)
    hits.sort(key=lambda r: _eu_date(r.get("marketing_authorisation_date")) or "", reverse=True)
    flags = ("orphan_medicine", "biosimilar", "generic", "conditional_approval",
             "accelerated_assessment", "additional_monitoring", "prime_priority_medicine")
    return {
        "query": query,
        "match_count": len(hits),
        "medicines": [{
            "name": r.get("name_of_medicine"),
            "active_substance": r.get("active_substance"),
            "status": r.get("medicine_status"),
            "holder": r.get("marketing_authorisation_developer_applicant_holder"),
            "authorised": _eu_date(r.get("marketing_authorisation_date")),
            "atc_code": r.get("atc_code_human"),
            "therapeutic_area": r.get("therapeutic_area_mesh"),
            "indication": _plain(r.get("therapeutic_indication"), 800),
            "flags": [f for f in flags if (r.get(f) or "").lower() == "yes"],
            "url": r.get("medicine_url"),
        } for r in hits[:limit]],
        "source": "European Medicines Agency, medicines data file",
    }


async def drug_shortages(conn: Connection, db, args: dict) -> dict:
    """FDA drug shortage list entries for a drug: status, availability, company, dates."""
    name = _name(args)
    limit = _limit(args, 10, 50)
    status = str(args.get("status") or "").strip().title()
    clean = name.replace('"', "")
    search = f'generic_name:"{clean}" OR openfda.brand_name:"{clean}" OR openfda.generic_name:"{clean}"'
    if status:
        search = f"({search}) AND status:\"{status}\""

    async def _loader():
        data = await _get_json(f"{FDA_BASE}/drug/shortages.json", {"search": search, "limit": limit})
        rows = data.get("results") or []
        return {
            "name": name,
            "in_shortage_list": bool(rows),
            "entries": [{
                "generic_name": r.get("generic_name"),
                "presentation": r.get("presentation"),
                "status": r.get("status"),
                "availability": r.get("availability"),
                "company": r.get("company_name"),
                "therapeutic_category": r.get("therapeutic_category"),
                "first_posted": r.get("initial_posting_date"),
                "updated": r.get("update_date"),
                "reason": r.get("shortage_reason"),
                "related_info": r.get("related_info"),
            } for r in rows],
            "source": "FDA Drug Shortages (openFDA)",
        }

    return await cached("medicines", conn.id, "drug_shortages", TTL_MEDIUM, _loader,
                        args={"q": search, "limit": limit})


async def atc_classification(conn: Connection, db, args: dict) -> dict:
    """WHO ATC therapeutic classes for a drug (via NIH RxClass)."""
    name = _name(args)

    async def _loader():
        data = await _get_json(f"{RXNAV_BASE}/rxclass/class/byDrugName.json",
                               {"drugName": name, "relaSource": "ATC"})
        seen, classes = set(), []
        for info in (data.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo", []) or []:
            item = info.get("rxclassMinConceptItem") or {}
            key = item.get("classId")
            if key and key not in seen:
                seen.add(key)
                classes.append({"atc_code": key, "class": item.get("className"),
                                "level": item.get("classType"),
                                "via": (info.get("minConcept") or {}).get("name")})
        classes.sort(key=lambda c: c["atc_code"])
        return {"name": name, "classes": classes, "source": "WHO ATC via NIH RxClass"}

    return await cached("medicines", conn.id, "atc_classification", TTL_LONG, _loader, args={"name": name})


async def patient_drug_info(conn: Connection, db, args: dict) -> dict:
    """MedlinePlus plain-language drug information pages for a drug name."""
    name = _name(args)
    language = "es" if str(args.get("language") or "").lower().startswith("es") else "en"

    async def _loader():
        ids = ((await _get_json(f"{RXNAV_BASE}/rxcui.json", {"name": name, "search": 2}))
               .get("idGroup") or {}).get("rxnormId") or []
        if not ids:
            return {"name": name, "found": False, "note": "No RxNorm concept for that name; try spelling_suggestions."}
        data = await _get_json(MEDLINEPLUS_CONNECT, {
            "mainSearchCriteria.v.cs": "2.16.840.1.113883.6.88",
            "mainSearchCriteria.v.c": ids[0],
            "knowledgeResponseType": "application/json",
            "informationRecipient.languageCode.c": language,
        })
        entries = (data.get("feed") or {}).get("entry") or []
        pages = [{
            "title": (e.get("title") or {}).get("_value"),
            "url": next((l.get("href") for l in e.get("link") or []), "").split("?")[0] or None,
            "summary": _plain((e.get("summary") or {}).get("_value"), 1500),
        } for e in entries]
        return {"name": name, "rxcui": ids[0], "found": bool(pages), "pages": pages,
                "source": "MedlinePlus (US National Library of Medicine)"}

    return await cached("medicines", conn.id, "patient_drug_info", TTL_LONG, _loader,
                        args={"name": name, "lang": language})


async def europe_pmc_search(conn: Connection, db, args: dict) -> dict:
    """Europe PMC literature search, open-access by default, with citation counts."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ConnectorError("query is required, e.g. 'dapagliflozin heart failure'.")
    if args.get("open_access_only", True):
        query = f"({query}) AND OPEN_ACCESS:y"
    params = {"query": query, "format": "json", "resultType": "core",
              "pageSize": _limit(args, 10, 50)}
    if args.get("sort") == "newest":
        params["sort"] = "FIRST_PDATE_D desc"
    elif args.get("sort") == "most_cited":
        params["sort"] = "CITED desc"
    if args.get("cursor"):
        params["cursorMark"] = str(args["cursor"])

    async def _loader():
        data = await _get_json(f"{EUROPE_PMC}/search", params)
        rows = [{
            "id": r.get("id"),
            "pmid": r.get("pmid"),
            "pmcid": r.get("pmcid"),
            "title": r.get("title"),
            "journal": (((r.get("journalInfo") or {}).get("journal") or {}).get("title")),
            "year": r.get("pubYear"),
            "authors": r.get("authorString"),
            "doi": r.get("doi"),
            "open_access": r.get("isOpenAccess") == "Y",
            "cited_by": r.get("citedByCount"),
            "abstract": _plain(r.get("abstractText"), 1200),
            "url": f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}",
        } for r in (data.get("resultList") or {}).get("result") or []]
        return {"query": query, "total_count": data.get("hitCount"), "articles": rows,
                "next_cursor": data.get("nextCursorMark"), "source": "Europe PMC"}

    return await cached("medicines", conn.id, "europe_pmc_search", TTL_MEDIUM, _loader, args=params)


async def europe_pmc_full_text(conn: Connection, db, args: dict) -> dict:
    """The full text of an open-access paper, section by section (capped)."""
    pmcid = str(args.get("pmcid") or "").strip().upper()
    if not re.fullmatch(r"PMC\d+", pmcid):
        raise ConnectorError("pmcid must look like PMC1234567 (from europe_pmc_search).")
    max_chars = _limit({"limit": args.get("max_chars")}, 20000, 60000)

    async def _loader():
        url = f"{EUROPE_PMC}/{pmcid}/fullTextXML"
        try:
            async with limit_for(url):
                res = await http_get(url, headers={"User-Agent": _HEADERS["User-Agent"]})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
        if res.status_code == 404:
            return {"pmcid": pmcid, "found": False,
                    "note": "No open-access full text for that id; the abstract may still be on PubMed."}
        if res.status_code >= 400:
            raise ConnectorError(f"Europe PMC returned {res.status_code}.")
        try:
            root = ET.fromstring(res.content)
        except ET.ParseError:
            raise ConnectorError("Europe PMC returned unreadable XML.")
        title = " ".join("".join(root.find(".//article-title").itertext()).split()) \
            if root.find(".//article-title") is not None else None
        sections, used = [], 0
        for sec in root.findall(".//body/sec"):
            head = sec.find("title")
            text = " ".join(" ".join(p.itertext()) for p in sec.iter("p"))
            text = " ".join(text.split())
            if not text:
                continue
            room = max_chars - used
            if room <= 0:
                break
            sections.append({"heading": "".join(head.itertext()).strip() if head is not None else None,
                             "text": text[:room]})
            used += min(len(text), room)
        return {"pmcid": pmcid, "found": True, "title": title, "sections": sections,
                "truncated": used >= max_chars,
                "url": f"https://europepmc.org/article/PMC/{pmcid}", "source": "Europe PMC"}

    return await cached("medicines", conn.id, "europe_pmc_full_text", TTL_LONG, _loader,
                        args={"id": pmcid, "max": max_chars})

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
    "fda_approval_history": {
        "description": "Drugs@FDA: each US application for a drug -- sponsor, original approval date, products with strengths and marketing status, and recent actions with links to labels and approval letters.",
        "input": {"type": "object", "properties": dict(_NAME_LIMIT), "required": ["name"], "additionalProperties": False},
    },
    "orange_book_patents": {
        "description": "FDA Orange Book: patents (with expiry, substance/product claims, use codes) and regulatory exclusivities per product, plus the latest listed patent expiry.",
        "input": {"type": "object", "properties": dict(_NAME_PROP), "required": ["name"], "additionalProperties": False},
    },
    "ema_medicines": {
        "description": "EU medicines from the European Medicines Agency: authorisation status and date, holder, ATC code, indication, and flags such as orphan, biosimilar or conditional approval.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Medicine or active substance; an indication with search_indications."},
                "search_indications": {"type": "boolean"},
                "status": {"type": "string", "description": "e.g. Authorised, Withdrawn, Refused."},
                "limit": {"type": "integer", "description": "1-50. Default 10."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "drug_shortages": {
        "description": "FDA drug shortage list: whether a drug is (or was) in shortage, availability, company, reason and dates.",
        "input": {
            "type": "object",
            "properties": {**_NAME_LIMIT, "status": {"type": "string", "enum": ["Current", "Resolved", "To Be Discontinued"]}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "atc_classification": {
        "description": "WHO ATC therapeutic classification codes and class names for a drug.",
        "input": {"type": "object", "properties": dict(_NAME_PROP), "required": ["name"], "additionalProperties": False},
    },
    "patient_drug_info": {
        "description": "MedlinePlus plain-language drug pages (uses, precautions) for patient-facing content, in English or Spanish.",
        "input": {
            "type": "object",
            "properties": {**_NAME_PROP, "language": {"type": "string", "enum": ["en", "es"]}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "europe_pmc_search": {
        "description": "Europe PMC literature search (open-access only by default): title, journal, year, authors, DOI, citation count, abstract and PMCID for full text.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "open_access_only": {"type": "boolean", "description": "Default true."},
                "sort": {"type": "string", "enum": ["relevance", "newest", "most_cited"]},
                "limit": {"type": "integer", "description": "1-50. Default 10."},
                "cursor": {"type": "string", "description": "next_cursor from a previous call."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "europe_pmc_full_text": {
        "description": "Full text of an open-access paper, section by section, capped at max_chars.",
        "input": {
            "type": "object",
            "properties": {"pmcid": {"type": "string", "description": "e.g. PMC13527552"},
                           "max_chars": {"type": "integer", "description": "Up to 60000. Default 20000."}},
            "required": ["pmcid"],
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
    "search_clinical_trials": search_clinical_trials,
    "get_clinical_trial": get_clinical_trial,
    "search_pubmed": search_pubmed,
    "get_pubmed_abstracts": get_pubmed_abstracts,
    "dailymed_labels": dailymed_labels,
    "india_approved_drugs": india_approved_drugs,
    "india_essential_medicines": india_essential_medicines,
    "india_ceiling_prices": india_ceiling_prices,
    "india_drug_profile": india_drug_profile,
    "fda_approval_history": fda_approval_history,
    "orange_book_patents": orange_book_patents,
    "ema_medicines": ema_medicines,
    "drug_shortages": drug_shortages,
    "atc_classification": atc_classification,
    "patient_drug_info": patient_drug_info,
    "europe_pmc_search": europe_pmc_search,
    "europe_pmc_full_text": europe_pmc_full_text,
}

registry.register(
    Connector(
        slug="medicines",
        label="Medicines & Drug Data",
        auth="api_key",
        cred_fields=[],
        catalog=CATALOG,
        handlers=HANDLERS,
        description=('Drug facts and evidence for pharma content: FDA labels, approvals, Orange Book '
                     'patents, shortages, adverse events and recalls; EMA (EU) medicines; WHO ATC classes; '
                     'MedlinePlus patient pages; ClinicalTrials.gov, PubMed and Europe PMC full text; plus '
                     'India -- CDSCO approvals, NLEM 2022 and NPPA ceiling prices. No API key.'),
        category='Reference',
    )
)
