"""Open-access discovery: Unpaywall, Europe PMC, PMC.

These are metadata lookups only (no paywall interaction).  All HTTP goes out
*without* the machine's proxy so that requests originate from the campus IP.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import requests


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # never use http(s)_proxy: entitlement is IP based
    s.headers.update({
        "User-Agent": "pubmed-dl/0.1 (academic literature retrieval; contact: yangzs@csu.edu.cn)",
        "Accept": "application/json",
    })
    return s


@dataclass
class OaCandidate:
    url: str
    source: str
    referer: str | None = None


def unpaywall(doi: str, email: str, sess: requests.Session | None = None) -> list[OaCandidate]:
    if not doi:
        return []
    s = sess or session()
    try:
        r = s.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    out: list[OaCandidate] = []
    locs = []
    if data.get("best_oa_location"):
        locs.append(data["best_oa_location"])
    locs.extend(data.get("oa_locations") or [])
    seen = set()
    for loc in locs:
        url = loc.get("url_for_pdf") or loc.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(OaCandidate(url=url, source=f"unpaywall:{loc.get('host_type','?')}"))
    return out


def epmc_pdf_url(pmcid: str) -> str:
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid.lstrip("PMCpmc")
    return f"https://europepmc.org/api/getPdf?pmcid={pmcid}"


def europepmc_pdf_candidates(pmcid: str, sess: requests.Session | None = None) -> list[OaCandidate]:
    """Europe PMC renders a PDF for open-access PMC articles."""
    if not pmcid:
        return []
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid.lstrip("PMCpmc")
    return [
        OaCandidate(url=f"https://europepmc.org/api/getPdf?pmcid={pmcid}",
                    source="europepmc:render", referer="https://europepmc.org/"),
        OaCandidate(url=f"https://europepmc.org/articles/{pmcid}?pdf=render",
                    source="europepmc:render2", referer="https://europepmc.org/"),
    ]


def pmc_pdf_candidates(pmcid: str) -> list[OaCandidate]:
    if not pmcid:
        return []
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = "PMC" + pmcid.lstrip("PMCpmc")
    return [
        OaCandidate(url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
                    source="pmc:pdf", referer=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"),
    ]


def europepmc_metadata(doi: str, pmid: str = "", sess: requests.Session | None = None) -> dict:
    s = sess or session()
    query = f'DOI:"{doi}"' if doi else f"EXT_ID:{pmid}"
    try:
        r = s.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                  params={"query": query, "format": "json", "resultType": "core", "pageSize": 1},
                  timeout=20)
        hits = r.json().get("resultList", {}).get("result", [])
        return hits[0] if hits else {}
    except Exception:
        return {}


def oa_candidates(doi: str, pmcid: str, pmid: str, email: str) -> list[OaCandidate]:
    """All plausible open-access PDF locations, cheap-to-expensive."""
    out: list[OaCandidate] = []
    out.extend(pmc_pdf_candidates(pmcid))
    out.extend(europepmc_pdf_candidates(pmcid))
    out.extend(unpaywall(doi, email))
    return out
