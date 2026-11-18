"""MCP server (stdio): the `pubmed_dl.api` facade as agent tools.

    pip install -e '.[mcp]'
    pubmed-dl-mcp                 # or: python -m pubmed_dl.mcp_server

Client config (Claude Desktop / Claude Code / Codex / any stdio client):

    {"mcpServers": {"pubmed-dl": {"command": "/abs/path/.venv/bin/pubmed-dl-mcp"}}}

Notes:
  * The pipeline is blocking (Playwright sync API + curl), so every tool call
    is offloaded with ``anyio.to_thread.run_sync``; the event loop stays free
    for ping/cancel while a download runs.
  * Only one call may own the Chrome profile/CDP port at a time; a second
    concurrent download gets an error instead of fighting over port 9222.
  * stdout belongs to the JSON-RPC stream: nothing here prints.
"""
from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated, Any

from anyio import to_thread

try:
    from mcp.server.fastmcp import FastMCP
    from pydantic import Field
except ModuleNotFoundError as exc:  # pragma: no cover - import-time guard
    raise ModuleNotFoundError(
        "the MCP server needs the optional dependency: pip install 'pubmed-dl[mcp]'"
    ) from exc

from . import api

__all__ = ["mcp", "main"]

log = logging.getLogger("pubmed_dl.mcp")

mcp = FastMCP(
    "pubmed_dl_mcp",
    instructions=(
        "Retrieve PubMed PDFs the institution is entitled to (campus egress IP, no proxy). "
        "Downloads drive a real Chrome that this server owns. Runs are resumable: state lives "
        "in <out>/state*.jsonl and re-calling skips records already done. "
        "status=blocked_bot means an anti-bot wall the tool will not defeat; ask the human to "
        "run `pubmed-dl login` on the desktop once, then retry with retry_failed=true."
    ),
)

# A Chrome profile and its CDP port are single-owner resources.
_browser_slot = threading.Lock()


@asynccontextmanager
async def _exclusive_browser():
    if not _browser_slot.acquire(blocking=False):
        raise RuntimeError(
            "another download/probe is already running in this server; wait for it to finish "
            "(the Chrome profile and CDP port cannot be shared)"
        )
    try:
        yield
    finally:
        _browser_slot.release()


# -- tools ----------------------------------------------------------------
@mcp.tool(
    name="pubmed_download",
    annotations={
        "title": "Download PubMed PDFs by DOI/PMID",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def pubmed_download(
    identifiers: Annotated[
        list[str],
        Field(min_length=1, max_length=500,
              description="DOIs or PMIDs, e.g. ['10.3389/fgene.2024.1401315', '33423259']"),
    ],
    out: Annotated[str, Field(description="Output directory (created if missing)")] = "downloads",
    limit: Annotated[int | None,
                     Field(ge=1, le=500, description="Process at most N records in this call")] = None,
    retry_failed: Annotated[bool, Field(description="Retry records that failed before")] = False,
    attempt_walled: Annotated[
        bool, Field(description="Also try publishers known to wall this route (needs a prior login)")] = False,
    oa_first: Annotated[bool, Field(description="Try repositories/PMC before the publisher")] = False,
) -> dict[str, Any]:
    """Download the PDFs of the given DOIs/PMIDs into an output directory.

    Uses the institution's subscription first (campus egress IP), then open-access
    copies, then publisher article pages. Validates magic bytes, page count and
    title overlap before a file counts as `ok`. Re-calling with the same `out`
    skips records already downloaded, so a batch can be resumed.

    Returns `counts` (status -> n), `ok`, `failed`, `out_dir`, `index_csv`,
    `bib_path`, `rows` (per-record status, capped at 50), `rows_total`.
    `hint` appears when some records hit an anti-bot wall.
    """
    try:
        res = await _blocking(
            api.download, identifiers, out, limit=limit, retry_failed=retry_failed,
            attempt_walled=attempt_walled, oa_first=oa_first,
        )
    except Exception as exc:
        return _error(exc)
    return _download_payload(res)


@mcp.tool(
    name="pubmed_download_csv",
    annotations={
        "title": "Download PDFs for a PubMed CSV export",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def pubmed_download_csv(
    csv_path: Annotated[str, Field(min_length=1,
                                   description="PubMed CSV export (columns PMID, Title, DOI, ...)")],
    out: Annotated[str, Field(description="Output directory (created if missing)")] = "downloads",
    prefix: Annotated[str | None,
                      Field(description="Comma separated DOI prefixes, e.g. '10.3390,10.3389'")] = None,
    only_pmcid: Annotated[bool, Field(description="Only records that carry a PMCID")] = False,
    shard: Annotated[str | None,
                     Field(description="Shard selector 'i/n' for parallel workers")] = None,
    limit: Annotated[int | None,
                     Field(ge=1, le=5000, description="Process at most N records in this call")] = None,
    retry_failed: Annotated[bool, Field(description="Retry records that failed before")] = False,
    attempt_walled: Annotated[
        bool, Field(description="Also try publishers known to wall this route (needs a prior login)")] = False,
    oa_first: Annotated[bool, Field(description="Try repositories/PMC before the publisher")] = False,
) -> dict[str, Any]:
    """Download PDFs for every record of a PubMed CSV export (optionally filtered).

    Same resolution order and validation as `pubmed_download`. Batch it with
    `limit`; `shard` ('i/n') is how the CLI runs several Chrome instances in
    parallel, each with its own state file.

    Returns `counts`, `ok`, `failed`, `out_dir`, `index_csv`, `bib_path`,
    `rows` (capped at 50), `rows_total`, and `hint` on anti-bot walls.
    """
    try:
        res = await _blocking(
            api.download_csv, csv_path, out, prefix=prefix, only_pmcid=only_pmcid,
            shard=shard, limit=limit, retry_failed=retry_failed,
            attempt_walled=attempt_walled, oa_first=oa_first,
        )
    except Exception as exc:
        return _error(exc)
    return _download_payload(res)


@mcp.tool(
    name="pubmed_report",
    annotations={
        "title": "Summarise a download run",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def pubmed_report(
    out: Annotated[str, Field(description="Run directory to summarise")] = "downloads",
    csv: Annotated[str | None,
                   Field(description="Optional CSV; PDFs on disk missing from state get indexed")] = None,
) -> dict[str, Any]:
    """Summarise a run directory and refresh its derived files.

    Reads `state*.jsonl`, then rewrites `index.csv`, `references.bib`,
    `missing.csv` and `summary.md` from it.

    Returns `counts` (status -> n) and `per_prefix` (DOI prefix -> {status: n}).
    """
    try:
        return await _blocking(api.report, out, csv=csv)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="pubmed_probe",
    annotations={
        "title": "Probe one DOI through the publisher chain",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def pubmed_probe(
    doi: Annotated[str, Field(min_length=1, description="DOI to resolve, e.g. 10.1002/jum.15266")],
    out: Annotated[str, Field(description="Run directory whose state/logs to use")] = "downloads",
    title: Annotated[str, Field(description="Optional title, used for validation")] = "",
    pmcid: Annotated[str, Field(description="Optional PMCID, enables the Europe PMC path")] = "",
) -> dict[str, Any]:
    """Resolve a single DOI and report what the pipeline sees, without saving it.

    Useful for deciding whether a publisher is reachable from this network
    before committing to a batch.

    Returns `status` (ok | blocked_bot | not_entitled | no_pdf | error),
    `source` (which path produced it), `detail`, `bytes`, `magic`.
    """
    try:
        return await _blocking(api.probe, doi, out, title=title, pmcid=pmcid)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    name="pubmed_verify",
    annotations={
        "title": "Verify downloaded PDFs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def pubmed_verify(
    out: Annotated[str, Field(description="Run directory to verify")] = "downloads",
    min_sim: Annotated[float, Field(ge=0.0, le=1.0,
                                    description="Minimum title/first-page word overlap to pass")] = 0.35,
    csv: Annotated[str | None, Field(description="Optional CSV; reconcile disk files first")] = None,
) -> dict[str, Any]:
    """Re-check every `ok` PDF: %PDF magic, page count, and title overlap.

    Returns `ok`, `suspect`, `missing` counts and `low`, the worst
    (similarity, DOI, filename) triples, at most 20.
    """
    try:
        return await _blocking(api.verify, out, min_sim=min_sim, csv=csv)
    except Exception as exc:
        return _error(exc)


# -- plumbing -------------------------------------------------------------
async def _blocking(fn, *args, **kwargs):
    """Run a blocking pipeline call off the event loop, owning the browser slot."""
    async with _exclusive_browser():
        return await to_thread.run_sync(partial(fn, *args, **kwargs))


def _download_payload(res: api.DownloadResult, shown: int = 50) -> dict[str, Any]:
    rows = res.rows
    payload: dict[str, Any] = {
        "counts": res.counts,
        "ok": res.ok,
        "failed": res.failed,
        "out_dir": str(res.out_dir),
        "index_csv": str(res.index_csv),
        "bib_path": str(res.bib_path),
        "rows": [_slim(r) for r in rows[:shown]],
        "rows_total": len(rows),
    }
    if res.counts.get("blocked_bot"):
        payload["hint"] = (
            "blocked_bot: an anti-bot wall. Ask the user to run "
            "`pubmed-dl login --urls <publisher>` once on the desktop, then call again "
            "with retry_failed=true."
        )
    return payload


def _slim(row: dict) -> dict:
    return {k: row.get(k) for k in ("pmid", "doi", "status", "source", "pages", "path")}


def _error(exc: BaseException) -> dict[str, Any]:
    hint = ""
    if isinstance(exc, RuntimeError) and "already running" in str(exc):
        hint = "wait for the in-flight call; do not start a second download"
    elif isinstance(exc, ModuleNotFoundError):
        hint = "pip install 'pubmed-dl[mcp]'"
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "hint": hint}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
