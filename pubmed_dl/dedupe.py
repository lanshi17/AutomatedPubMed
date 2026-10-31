"""Deduplicate an external PDF collection against the local corpus.

Three keys, strongest first:

1. ``SHA256`` — the same bytes, therefore the same file.
2. ``PMID`` — the same PubMed record (covers a re-scan / different publisher copy).
3. ``DOI`` (case-folded) — the same article reached through a different record.

Everything that matches none of these is reported as unique so it can be merged
in. For duplicate articles whose bytes differ, the larger file is flagged as the
preferred copy (a full scan beats a truncated one), but nothing is deleted here:
the module only reports.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUR_COLUMNS = ["PMID", "DOI", "Title", "Journal", "Year", "FirstAuthor", "Source",
               "Pages", "Bytes", "File", "SHA256"]
ACTION_COLUMNS = ["pmid", "doi", "zip_file", "zip_bytes", "sha256",
                  "match", "our_file", "our_bytes", "our_sha256", "prefer", "note"]

_PMID_RE = re.compile(r"PMID(\d+)")


def norm_doi(value: str) -> str:
    return (value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_our_corpus(index_csv: Path) -> tuple[dict, dict, dict]:
    """Return (by_pmid, by_doi, by_sha) lookups built from the local index."""
    by_pmid: dict[str, dict] = {}
    by_doi: dict[str, dict] = {}
    by_sha: dict[str, dict] = {}
    with index_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pmid = (row.get("PMID") or "").strip()
            doi = norm_doi(row.get("DOI") or "")
            sha = (row.get("SHA256") or "").strip().lower()
            if pmid:
                by_pmid.setdefault(pmid, row)
            if doi:
                by_doi.setdefault(doi, row)
            if sha:
                by_sha.setdefault(sha, row)
    return by_pmid, by_doi, by_sha


def load_zip_records(root: Path) -> list[dict]:
    """One dict per PDF in the archive, with DOI/PMCID taken from fulltext_index.csv."""
    meta: dict[str, dict] = {}
    index_csv = root / "fulltext_index.csv"
    if index_csv.exists():
        with index_csv.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                pmid = (row.get("pmid") or "").strip()
                if pmid:
                    meta[pmid] = row

    records = []
    for path in sorted((root / "pdf").glob("*.pdf")):
        m = _PMID_RE.search(path.name)
        pmid = m.group(1) if m else ""
        row = meta.get(pmid, {})
        records.append({
            "path": path,
            "name": path.name,
            "pmid": pmid,
            "doi": norm_doi(row.get("doi") or ""),
            "pmcid": (row.get("pmcid") or "").strip(),
            "bytes": path.stat().st_size,
        })
    return records


def run(zip_root: Path, our_index: Path, out_dir: Path, workers: int = 8, log=print) -> dict:
    by_pmid, by_doi, by_sha = load_our_corpus(our_index)
    records = load_zip_records(zip_root)
    log(f"local corpus: {len(by_pmid)} PMIDs / {len(by_doi)} DOIs / {len(by_sha)} hashes")
    log(f"archive: {len(records)} PDFs")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        hashes = list(pool.map(lambda r: sha256_of(r["path"]), records))
    for rec, sha in zip(records, hashes):
        rec["sha256"] = sha

    out_dir.mkdir(parents=True, exist_ok=True)
    actions = []
    stats = defaultdict(int)
    for rec in records:
        match, our, prefer, note = "", None, "", ""
        if rec["sha256"] in by_sha:
            match = "same_bytes"
            our = by_sha[rec["sha256"]]
        elif rec["pmid"] and rec["pmid"] in by_pmid:
            match = "same_pmid"
            our = by_pmid[rec["pmid"]]
        elif rec["doi"] and rec["doi"] in by_doi:
            match = "same_doi"
            our = by_doi[rec["doi"]]
        if our:
            ours_bytes = int(our.get("Bytes") or 0)
            if rec["bytes"] > ours_bytes * 1.15:
                prefer, note = "zip", f"zip copy is {rec['bytes']} B vs local {ours_bytes} B"
            elif ours_bytes > rec["bytes"] * 1.15:
                prefer, note = "local", f"local copy is {ours_bytes} B vs zip {rec['bytes']} B"
            else:
                prefer, note = "either", "comparable size"
            if match != "same_bytes" and (our.get("SHA256") or "").lower() != rec["sha256"]:
                note += "; bytes differ (re-scan or different rendering)"
        stats[match or "unique"] += 1
        actions.append({
            "pmid": rec["pmid"], "doi": rec["doi"], "zip_file": rec["name"],
            "zip_bytes": rec["bytes"], "sha256": rec["sha256"],
            "match": match or "unique",
            "our_file": Path(our.get("File") or "").name if our else "",
            "our_bytes": our.get("Bytes", "") if our else "",
            "our_sha256": our.get("SHA256", "") if our else "",
            "prefer": prefer, "note": note,
        })

    actions_csv = out_dir / "dedupe_actions.csv"
    with actions_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ACTION_COLUMNS)
        writer.writeheader()
        writer.writerows(actions)

    unique = [a for a in actions if a["match"] == "unique"]
    with (out_dir / "unique_files.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ACTION_COLUMNS)
        writer.writeheader()
        writer.writerows(unique)

    summary = {
        "archive_pdfs": len(records),
        "local_pdfs": len(by_pmid) if by_pmid else 0,
        "match_counts": dict(stats),
        "unique_pdfs": len(unique),
        "unique_bytes": sum(int(a["zip_bytes"]) for a in unique),
        "prefer_local": sum(1 for a in actions if a["prefer"] == "local"),
        "prefer_zip": sum(1 for a in actions if a["prefer"] == "zip"),
    }
    (out_dir / "dedupe_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for key, value in summary.items():
        log(f"  {key}: {value}")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dedupe an external PDF set against the local corpus")
    ap.add_argument("--zip-root", required=True, help="extracted archive directory (holds pdf/)")
    ap.add_argument("--our-index", default="downloads/index.csv")
    ap.add_argument("--out-dir", default="dedupe")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    run(Path(args.zip_root), Path(args.our_index), Path(args.out_dir), workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
