"""Command line interface: python -m pubmed_dl <command>.

Thin formatting layer only; the pipeline itself lives in `pubmed_dl.api`.
"""
from __future__ import annotations

import argparse
import sys

from . import api


def cmd_run(args) -> int:
    extra = {"wall_wait_s": args.wall_wait} if args.wall_wait is not None else {}
    if args.browser_act is not None:
        extra["browser_act"] = args.browser_act or None
    res = api.download_csv(
        args.csv, args.out, prefix=args.prefix, only_pmcid=args.only_pmcid,
        shard=args.shard, limit=args.limit, retry_failed=args.retry_failed,
        attempt_walled=args.attempt_walled, oa_first=args.oa_first,
        interactive=args.interactive, display=args.display, port=args.port,
        log=print, **extra,
    )
    print("\n== batch result ==")
    for k, v in sorted(res.counts.items()):
        print(f"  {k:12s} {v}")
    return 0


def cmd_report(args) -> int:
    st = api.report(args.out, csv=args.csv)
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
    return 0


def cmd_login(args) -> int:
    """Open a visible browser on the user's desktop for manual sign-in/captcha."""
    cfg = api.make_config(args.out, display=":0", interactive=True)
    print("A Chrome window will open on your desktop.",
          "Complete any sign-in or 'I am human' check,",
          "then close the window when you are done.", sep="\n")
    b = api.start_browser(cfg)
    urls = args.urls or ["https://www.sciencedirect.com/"]
    for url in urls:
        b.goto(url, wait_ms=4000)
        print("opened:", url)
    print("\n请等到页面真正显示文章内容（不再出现“请稍候”/“Are you a robot?”）再回车。")
    input("Press Enter here when finished... ")
    status = {}
    for url in urls:
        try:
            ctx = b.context
            names = {c["name"] for c in ctx.cookies(url)} if ctx else set()
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
    out = api.probe(args.doi, args.out, csv=args.csv, pmcid=args.pmcid or "",
                    title=args.title or "", display=args.display, port=args.port)
    print("status:", out["status"], "| source:", out["source"], "| detail:", out["detail"])
    if out["bytes"]:
        print("bytes:", out["bytes"], "magic:", out["magic"])
    return 0


def cmd_verify(args) -> int:
    """Re-check every recorded PDF: magic bytes, page count, title match."""
    res = api.verify(args.out, min_sim=args.min_sim, csv=args.csv)
    print(f"verified ok: {res['ok']}  suspect: {res['suspect']}  missing: {res['missing']}")
    for sim, doi, name in res["low"]:
        print(f"  sim={sim} {doi} {name}")
    return 0


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
                   help="try repositories/PMC before the publisher")
    r.add_argument("--only-pmcid", action="store_true",
                   help="only records that carry a PMCID")
    r.add_argument("--attempt-walled", action="store_true",
                   help="also try publishers known to wall this route")
    r.add_argument("--wall-wait", type=float, default=None,
                   help="seconds to wait for a JS challenge to clear (default 60)")
    r.add_argument("--browser-act", default=None,
                   help="browser-act browser name; empty string disables the backend")
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
