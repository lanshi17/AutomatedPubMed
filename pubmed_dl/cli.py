"""Command line interface: python -m pubmed_dl <command>."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .browser import Browser
from .config import Config, env_defaults
from .downloader import Downloader
from .records import load_csv


def _browser(cfg: Config) -> Browser:
    b = Browser(port=cfg.port, display=cfg.display, profile=cfg.profile_dir,
                timeout_ms=cfg.timeout_ms)
    return b.start()


def cmd_run(args) -> int:
    defaults = env_defaults()
    shard, shards = 0, 1
    if args.shard:
        a, b = args.shard.split("/")
        shard, shards = int(a), int(b)
    base_port = args.port or defaults["port"]
    cfg = Config(
        out_dir=Path(args.out),
        csv_path=Path(args.csv) if args.csv else None,
        display=args.display or (":0" if args.interactive else defaults["display"]),
        port=base_port + shard,
        profile_dir=Path(f"/tmp/pubmed_dl_chrome_{base_port + shard}"),
        interactive=args.interactive,
        shard=shard, shards=shards,
        attempt_walled=args.attempt_walled,
        oa_first=args.oa_first,
    )
    records = load_csv(args.csv)
    if args.prefix:
        wanted = {p.strip().lower() for p in args.prefix.split(",")}
        records = [r for r in records if r.prefix in wanted]
    if shards > 1:
        records = records[shard::shards]
    if args.only_pmcid:
        records = [r for r in records if r.pmcid]
    if args.limit:
        records = records[:args.limit]
    browser = _browser(cfg)
    dl = Downloader(cfg, browser)
    try:
        counts = dl.run(records, retry_failed=args.retry_failed, log=print)
    finally:
        browser.close()
    dl.export_index()
    dl.export_bibtex()
    print("\n== batch result ==")
    for k, v in sorted(counts.items()):
        print(f"  {k:12s} {v}")
    return 0


def cmd_report(args) -> int:
    cfg = Config(out_dir=Path(args.out))
    dl = Downloader(cfg)
    if args.csv:
        dl.reconcile_disk(Path(args.csv))
    st = dl.stats()
    print("== overall ==")
    for k, v in sorted(st["counts"].items()):
        print(f"  {k:12s} {v}")
    print("\n== per DOI prefix ==")
    for pref, d in sorted(st["per_prefix"].items(), key=lambda kv: -sum(kv[1].values())):
        ok = d.get("ok", 0)
        total = sum(d.values())
        pct = 100.0 * ok / total if total else 0
        other = ", ".join(f"{k}:{v}" for k, v in d.items() if k != "ok")
        print(f"  {pref:12s} {ok:4d}/{total:<4d} ({pct:5.1f}%)  {other}")
    dl.export_index()
    dl.export_bibtex()
    dl.export_missing()
    (cfg.out_dir / "summary.md").write_text(dl.summary_markdown(), encoding="utf-8")
    return 0


def cmd_login(args) -> int:
    """Open a visible browser on the user's desktop for manual sign-in/captcha."""
    port = int(os.environ.get("PUBMED_DL_PORT", "9222"))
    cfg = Config(out_dir=Path(args.out), display=":0", interactive=True,
                 port=port, profile_dir=Path(f"/tmp/pubmed_dl_chrome_{port}"))
    print("A Chrome window will open on your desktop.",
          "Complete any sign-in or 'I am human' check,", 
          "then close the window when you are done.", sep="\n")
    b = Browser(port=cfg.port, display=":0", profile=cfg.profile_dir)
    b.start()
    urls = args.urls or ["https://www.sciencedirect.com/"]
    for url in urls:
        b.goto(url, wait_ms=4000)
        print("opened:", url)
    print("\n请等到页面真正显示文章内容（不再出现“请稍候”/“Are you a robot?”）再回车。")
    input("Press Enter here when finished... ")
    status = {}
    for url in urls:
        try:
            names = {c["name"] for c in b.context.cookies(url)}
            status[url] = sorted(n for n in names if "clearance" in n or n in
                                 ("SD_REMOTEACCESS", "ANONRA_COOKIE", "KARGER_SessionId"))
        except Exception:
            status[url] = []
    b.close()
    print("session profile saved in", cfg.profile_dir)
    for url, names in status.items():
        passed = any("clearance" in n for n in names) or bool(names)
        print(f"  {'OK ' if passed else 'NO '} {url}  cookies={names}")
    return 0


def cmd_probe(args) -> int:
    """Probe a single DOI and print what the pipeline sees."""
    defaults = env_defaults()
    cfg = Config(out_dir=Path(args.out), display=args.display or defaults["display"],
                 port=args.port or defaults["port"])
    records = load_csv(args.csv) if args.csv else []
    rec = next((r for r in records if r.doi == args.doi), None)
    if rec is None:
        from .records import Record
        rec = Record(pmid="", doi=args.doi, pmcid=args.pmcid or "", title=args.title or "",
                     authors="", citation="", journal="", year="", first_author="")
    b = _browser(cfg)
    try:
        from .publishers import resolve
        result = resolve(rec, b)
        print("status:", result.status, "| source:", result.source, "| detail:", result.detail)
        if result.data:
            print("bytes:", len(result.data), "magic:", result.data[:4])
    finally:
        b.close()
    return 0


def cmd_verify(args) -> int:
    """Re-check every recorded PDF: magic bytes, page count, title match."""
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    from pypdf import PdfReader
    cfg = Config(out_dir=Path(args.out))
    dl = Downloader(cfg)
    if getattr(args, "csv", None):
        dl.reconcile_disk(Path(args.csv))
    ok = bad = missing = 0
    low = []
    for row in dl.state.values():
        if row.get("status") != "ok":
            continue
        path = Path(row.get("path", ""))
        if not path.exists():
            missing += 1
            continue
        try:
            reader = PdfReader(str(path))
            pages = len(reader.pages)
            first = (reader.pages[0].extract_text() or "")[:2000]
            sim = _sim(row.get("title", ""), first)
            if pages >= 1 and path.read_bytes()[:4] == b"%PDF" and sim >= args.min_sim:
                ok += 1
            else:
                bad += 1
                low.append((sim, row.get("doi", ""), path.name))
        except Exception as exc:
            pages, first = _poppler_probe(path)
            sim = _sim(row.get("title", ""), first)
            if pages >= 1 and sim >= args.min_sim:
                ok += 1
            else:
                bad += 1
                low.append((sim, row.get("doi", ""), f"{path.name} ({repr(exc)[:40]})"))
    print(f"verified ok: {ok}  suspect: {bad}  missing: {missing}")
    for sim, doi, name in sorted(low)[:20]:
        print(f"  sim={sim} {doi} {name}")
    return 0


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
    from .records import title_similarity
    return round(title_similarity(a, b), 3)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pubmed_dl")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="download PDFs for records in a PubMed CSV")
    r.add_argument("--csv", required=True)
    r.add_argument("--out", default="downloads")
    r.add_argument("--limit", type=int)
    r.add_argument("--prefix", help="comma separated DOI prefixes, e.g. 10.1002,10.1007")
    r.add_argument("--retry-failed", action="store_true")
    r.add_argument("--shard", help="i/n shard selector for parallel workers")
    r.add_argument("--oa-first", action="store_true",
                   help="try repositories before the publisher")
    r.add_argument("--only-pmcid", action="store_true",
                   help="only records that have a PMCID (Europe PMC copies)")
    r.add_argument("--attempt-walled", action="store_true",
                   help="also try prefixes known to be behind anti-bot walls")
    r.add_argument("--interactive", action="store_true", help="use display :0")
    r.add_argument("--display")
    r.add_argument("--port", type=int)
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="summarise the run")
    rep.add_argument("--out", default="downloads")
    rep.add_argument("--csv", help="also index PDFs on disk missing from state")
    rep.set_defaults(func=cmd_report)

    lg = sub.add_parser("login", help="open a visible browser to sign in / clear a captcha once")
    lg.add_argument("--out", default="downloads")
    lg.add_argument("--urls", nargs="*")
    lg.set_defaults(func=cmd_login)

    pr = sub.add_parser("probe", help="probe one DOI through the resolver")
    pr.add_argument("--doi", required=True)
    pr.add_argument("--csv")
    pr.add_argument("--pmcid")
    pr.add_argument("--title")
    pr.add_argument("--out", default="downloads")
    pr.add_argument("--display")
    pr.add_argument("--port", type=int)
    pr.set_defaults(func=cmd_probe)

    vf = sub.add_parser("verify", help="re-check downloaded PDFs")
    vf.add_argument("--out", default="downloads")
    vf.add_argument("--min-sim", type=float, default=0.35)
    vf.add_argument("--csv", help="reconcile disk files before verifying")
    vf.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
