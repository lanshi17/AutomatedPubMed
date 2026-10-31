"""Recover DOIs for PubMed records whose export carries none.

Many older / non-English PubMed records have no DOI in the MEDLINE export even
though the journal article does carry one. Crossref's bibliographic search
finds the deposit from title + journal + year; we accept a hit only when the
title matches strongly and the year agrees, and we keep the journal name from
the PubMed record so downloads stay organised.

Nothing is downloaded here: the module only enriches metadata. Any wrong match
is caught later by the PDF title check in ``Downloader.validate``.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .records import Record, load_csv, title_similarity, normalize_title

UA = "pubmed_dl/1.0 (mailto:yangzs@csu.edu.cn)"
CROSSREF = "https://api.crossref.org/works"
COLUMNS = ["PMID", "Title", "Authors", "Citation", "First Author", "Journal/Book",
           "Publication Year", "Create Date", "PMCID", "NIHMS ID", "DOI"]


def _norm_year(value) -> str:
    return re.sub(r"\D", "", str(value or ""))[:4]


def _issued_year(item: dict) -> str:
    for field in ("issued", "published-print", "published-online"):
        parts = ((item.get(field) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            return str(parts[0])
    return ""


def _lookup(rec: Record, min_sim: float, year_slack: int, timeout: float) -> tuple[str, float, str]:
    """Return (doi, similarity, reason) for the best acceptable Crossref hit."""
    params = {
        "query.bibliographic": rec.title[:200],
        "rows": 8,
        "select": "DOI,title,issued,published-print,published-online,container-title,type",
    }
    # Deliberately no query.container-title: Crossref's field filter is very
    # strict and turns most journal hits into zero results.
    try:
        resp = requests.get(CROSSREF, params=params, headers={"User-Agent": UA},
                            timeout=timeout)
        if resp.status_code != 200:
            return "", 0.0, f"http {resp.status_code}"
        items = ((resp.json() or {}).get("message") or {}).get("items") or []
    except Exception as exc:  # network hiccup: retry once, then leave for a later pass
        try:
            time.sleep(0.5)
            resp = requests.get(CROSSREF, params=params, headers={"User-Agent": UA},
                                timeout=timeout)
            items = ((resp.json() or {}).get("message") or {}).get("items") or []
        except Exception:
            return "", 0.0, f"request: {exc.__class__.__name__}"

    best: tuple[str, float, str, int] = ("", 0.0, "no match", 0)
    for item in items:
        doi = (item.get("DOI") or "").strip()
        if not doi:
            continue
        title = (item.get("title") or [""])[0]
        sim = title_similarity(rec.title, title)
        if sim < min_sim:
            continue
        got_year = _issued_year(item)
        if rec.year and got_year and abs(int(got_year) - int(rec.year)) > year_slack:
            continue
        kind = (item.get("type") or "").lower()
        if kind in ("component", "dataset", "peer-review"):
            continue
        year_ok = 1 if (rec.year and got_year and got_year == rec.year) else 0
        # Prefer same-year deposits over a merely similar title: short titles
        # repeat across decades, and the year is what separates them.
        score = (year_ok, sim)
        if (score[0], score[1]) > (best[3], best[1]):
            best = (doi, sim, f"crossref:{kind or 'work'}", year_ok)
    return best[0], best[1], best[2]


def backfill(csv_path: str | Path, out_csv: str | Path, state_dir: str | Path | None = None,
             min_sim: float = 0.85, year_slack: int = 2, workers: int = 6,
             limit: int | None = None, log=print) -> dict:
    """Write an enriched CSV for records that have no DOI and no PMCID."""
    records = load_csv(csv_path)
    done_keys: set[str] = set()
    if state_dir:
        for path in Path(state_dir).glob("state*.jsonl"):
            for line in path.open(encoding="utf-8"):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("key") and row.get("status") == "ok":
                    done_keys.add(row["key"])

    todo = [r for r in records
            if not r.doi and not r.pmcid and r.pmid and r.key not in done_keys]
    if limit:
        todo = todo[:limit]
    log(f"records without DOI/PMCID: {len(records) - sum(1 for r in records if r.doi)} ; "
        f"to query: {len(todo)}")

    stats = {"queried": 0, "matched": 0, "unmatched": 0, "error": 0, "written": 0}

    def work(rec: Record):
        res = _lookup(rec, min_sim, year_slack, timeout=20.0)
        time.sleep(0.15)
        return rec, res

    enriched: list[tuple[Record, str, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rec, (doi, sim, reason) in pool.map(work, todo):
            stats["queried"] += 1
            if doi:
                stats["matched"] += 1
                enriched.append((rec, doi, sim))
            elif reason.startswith("request") or reason.startswith("http"):
                stats["error"] += 1
            else:
                stats["unmatched"] += 1
            if stats["queried"] % 200 == 0:
                log(f"  ... {stats['queried']}/{len(todo)} queried, {stats['matched']} matched")

    with Path(out_csv).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for rec, doi, _sim in enriched:
            writer.writerow({
                "PMID": rec.pmid, "Title": rec.title, "Authors": rec.authors,
                "Citation": rec.citation, "First Author": rec.first_author,
                "Journal/Book": rec.journal, "Publication Year": rec.year,
                "Create Date": "", "PMCID": "", "NIHMS ID": "", "DOI": doi,
            })
            stats["written"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Recover DOIs via Crossref title search")
    ap.add_argument("csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--state-dir", help="skip records already downloaded in this state dir")
    ap.add_argument("--min-sim", type=float, default=0.85)
    ap.add_argument("--year-slack", type=int, default=2)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    stats = backfill(args.csv, args.out, state_dir=args.state_dir,
                     min_sim=args.min_sim, year_slack=args.year_slack,
                     workers=args.workers, limit=args.limit)
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
