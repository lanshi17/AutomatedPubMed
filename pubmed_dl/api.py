"""Programmatic facade: the CLI pipeline without argparse or printing.

    from pubmed_dl import api
    res = api.download(["10.1002/jum.15266"], out="downloads")
    res.counts      # {'ok': 1}
    res.rows        # state rows written to state.jsonl
    res.index_csv   # downloads/index.csv

Two constraints inherited from the pipeline:

  * Playwright's sync API is thread-affine.  The ``Browser`` is created and
    used in the calling thread; do not hand a ``Browser`` to another thread.
  * ``Downloader.run`` caps each record with SIGALRM, which only exists on the
    main thread.  Called from a worker thread it silently relies on the
    per-request ``timeout_ms`` instead.
"""
from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .browser import Browser
from .config import Config, env_defaults
from .downloader import Downloader
from .publishers import resolve
from .records import Record, load_csv, title_similarity

__all__ = [
    "DownloadResult",
    "download",
    "download_csv",
    "make_config",
    "make_record",
    "probe",
    "report",
    "start_browser",
    "verify",
]

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


@dataclass
class DownloadResult:
    counts: dict[str, int]
    rows: list[dict]
    out_dir: Path
    index_csv: Path
    bib_path: Path
    stats: dict

    @property
    def ok(self) -> int:
        return self.counts.get("ok", 0)

    @property
    def failed(self) -> int:
        return sum(v for k, v in self.counts.items() if k != "ok")


# -- construction ---------------------------------------------------------
def make_config(out: str | Path = "downloads", *, csv: str | Path | None = None,
                shard: str | tuple[int, int] | None = None, port: int | None = None,
                display: str | None = None, interactive: bool = False,
                **overrides) -> Config:
    """Build a Config with the CLI's shard/port/profile conventions."""
    shard_i, shards = _parse_shard(shard)
    defaults = env_defaults()
    base_port = port or defaults["port"]
    # Chrome profiles grow to hundreds of MB each; keep them off tmpfs so a
    # dozen parallel workers cannot exhaust /tmp.
    base = Path(os.environ.get("PUBMED_DL_PROFILE_BASE", "/tmp"))
    return Config(
        out_dir=Path(out),
        csv_path=Path(csv) if csv else None,
        display=display or (":0" if interactive else defaults["display"]),
        port=base_port + shard_i,
        profile_dir=base / f"pubmed_dl_chrome_{base_port + shard_i}",
        interactive=interactive,
        shard=shard_i,
        shards=shards,
        browser_act_session="pubmed-pub" if shards == 1 else f"pubmed-pub-s{shard_i}",
        **overrides,
    )


def make_record(ident: str, *, pmcid: str = "", title: str = "", authors: str = "",
                citation: str = "", journal: str = "", year: str = "",
                first_author: str = "") -> Record:
    """Turn a DOI or PMID (bare, ``doi:``-prefixed, or doi.org URL) into a Record."""
    s = _strip_doi_prefix(ident)
    if not s:
        raise ValueError("empty identifier")
    if s.startswith("10."):
        doi, pmid = s, ""
    elif s.isdigit():
        doi, pmid = "", s
    else:
        raise ValueError(f"not a DOI or PMID: {ident!r}")
    return Record(pmid=pmid, doi=doi, pmcid=pmcid, title=title, authors=authors,
                  citation=citation, journal=journal, year=year,
                  first_author=first_author)


def start_browser(cfg: Config) -> Browser:
    return Browser(port=cfg.port, display=cfg.display, profile=cfg.profile_dir,
                   timeout_ms=cfg.timeout_ms).start()


def _make_act(cfg: Config, browser: Browser, *, session: str | None = None):
    """Build the browser-act backend when the named browser exists."""
    if not cfg.browser_act:
        return None
    try:
        from .browser_act import BrowserAct
        act = BrowserAct(browser=browser, name=cfg.browser_act,
                         session=session or cfg.browser_act_session,
                         wall_wait_s=cfg.wall_wait_s,
                         display=getattr(browser, "display", ":99"))
        return act if act.available() else None
    except Exception:
        return None


# -- download -------------------------------------------------------------
def download(identifiers, out: str | Path = "downloads", *, limit: int | None = None,
             retry_failed: bool = False, log=None, browser: Browser | None = None,
             **cfg_kw) -> DownloadResult:
    """Download PDFs for a list of DOI/PMID strings."""
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    records = [make_record(i) for i in identifiers]
    return _download(records, out, limit=limit, retry_failed=retry_failed,
                     log=log, browser=browser, **cfg_kw)


def download_csv(csv_path: str | Path, out: str | Path = "downloads", *,
                 prefix: str | None = None, only_pmcid: bool = False,
                 shard: str | tuple[int, int] | None = None,
                 limit: int | None = None, retry_failed: bool = False,
                 log=None, browser: Browser | None = None, **cfg_kw) -> DownloadResult:
    """Download PDFs for every record in a PubMed export CSV."""
    records = load_csv(csv_path)
    if prefix:
        wanted = {p.strip().lower() for p in prefix.split(",") if p.strip()}
        records = [r for r in records if r.prefix in wanted]
    if only_pmcid:
        records = [r for r in records if r.pmcid]
    shard_i, shards = _parse_shard(shard)
    if shards > 1:
        records = records[shard_i::shards]
    return _download(records, out, limit=limit, retry_failed=retry_failed,
                     log=log, browser=browser, shard=(shard_i, shards),
                     csv=csv_path, **cfg_kw)


def _download(records: list[Record], out, *, limit, retry_failed, log,
              browser, shard=None, **cfg_kw) -> DownloadResult:
    cfg = make_config(out, shard=shard, **cfg_kw)
    own = browser is None
    b = browser if browser is not None else start_browser(cfg)
    dl = Downloader(cfg, b)
    try:
        counts = dl.run(records, limit=limit, retry_failed=retry_failed, log=log or _quiet)
        rows = [dl.state[r.key] for r in records if r.key in dl.state]
        dl.export_index()
        dl.export_bibtex()
    finally:
        if own:
            b.close()
    return DownloadResult(counts=counts, rows=rows, out_dir=cfg.out_dir,
                          index_csv=cfg.index_csv, bib_path=cfg.bib_path,
                          stats=dl.stats())


# -- read-only commands ---------------------------------------------------
def report(out: str | Path = "downloads", *, csv: str | Path | None = None) -> dict:
    """Aggregate state, refresh index/BibTeX/missing list/summary.md."""
    cfg = make_config(out)
    dl = Downloader(cfg)
    if csv:
        dl.reconcile_disk(Path(csv))
    stats = dl.stats()
    dl.export_index()
    dl.export_bibtex()
    dl.export_missing()
    (cfg.out_dir / "summary.md").write_text(dl.summary_markdown(), encoding="utf-8")
    return stats


def probe(doi: str, out: str | Path = "downloads", *, csv: str | Path | None = None,
          pmcid: str = "", title: str = "", display: str | None = None,
          port: int | None = None) -> dict:
    """Resolve one DOI through the publisher chain and report what was seen."""
    cfg = make_config(out, display=display, port=port)
    rec = None
    if csv:
        rec = next((r for r in load_csv(csv) if r.doi == doi), None)
    if rec is None:
        rec = make_record(doi, pmcid=pmcid, title=title)
    b = start_browser(cfg)
    act = _make_act(cfg, b, session="pubmed-pub-probe")
    try:
        result = resolve(rec, b, timeout_ms=cfg.timeout_ms * 3,
                         wall_wait_s=cfg.wall_wait_s, act=act,
                         rounds=cfg.resolve_rounds,
                         wall_cooldown_s=cfg.wall_cooldown_s,
                         act_first=cfg.act_first)
    finally:
        if act:
            act.close()
        b.close()
    return {"status": result.status, "source": result.source,
            "detail": result.detail, "bytes": len(result.data or b""),
            "magic": (result.data or b"")[:4].decode("latin-1")}


def verify(out: str | Path = "downloads", *, min_sim: float = 0.35,
           csv: str | Path | None = None) -> dict:
    """Re-check every recorded PDF: magic bytes, page count, title overlap."""
    warnings.filterwarnings("ignore")
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    cfg = make_config(out)
    dl = Downloader(cfg)
    if csv:
        dl.reconcile_disk(Path(csv))
    ok = bad = missing = 0
    low: list[tuple[float, str, str]] = []
    for row in dl.state.values():
        if row.get("status") != "ok":
            continue
        path = Path(row.get("path", ""))
        if not path.exists():
            missing += 1
            continue
        # A DOI-only record carries no title, so the overlap gate cannot apply.
        need_sim = bool((row.get("title") or "").strip())
        try:
            reader = PdfReader(str(path))
            pages = len(reader.pages)
            first = (reader.pages[0].extract_text() or "")[:2000]
            sim = _sim(row.get("title", ""), first)
            if pages >= 1 and path.read_bytes()[:4] == b"%PDF" and (sim >= min_sim or not need_sim):
                ok += 1
            else:
                bad += 1
                low.append((sim, row.get("doi", ""), path.name))
        except Exception as exc:
            pages, first = _poppler_probe(path)
            sim = _sim(row.get("title", ""), first)
            if pages >= 1 and (sim >= min_sim or not need_sim):
                ok += 1
            else:
                bad += 1
                low.append((sim, row.get("doi", ""), f"{path.name} ({repr(exc)[:40]})"))
    return {"ok": ok, "suspect": bad, "missing": missing,
            "low": sorted(low)[:20]}


# -- helpers --------------------------------------------------------------
def _quiet(msg: str) -> None:
    return None


def _parse_shard(shard) -> tuple[int, int]:
    if shard is None:
        return 0, 1
    if isinstance(shard, str):
        a, b = shard.split("/")
        return int(a), int(b)
    a, b = shard
    return int(a), int(b)


def _strip_doi_prefix(s: str) -> str:
    s = (s or "").strip()
    low = s.lower()
    for p in _DOI_PREFIXES:
        if low.startswith(p):
            return s[len(p):].strip()
    return s


def _poppler_probe(path: Path) -> tuple[int, str]:
    import subprocess
    pages = 0
    text = ""
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split()[1])
        out = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(path), "-"],
                             capture_output=True, text=True, timeout=30)
        text = (out.stdout or "")[:2000]
    except Exception:
        pass
    return pages, text


def _sim(a: str, b: str) -> float:
    return round(title_similarity(a, b), 3)
