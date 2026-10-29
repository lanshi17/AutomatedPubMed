"""Download orchestration: state, validation, resume, indexing."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import signal
import random
import time
import warnings
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import sources
from .browser import Browser
from .config import Config
from .fetch import fetch_pdf
from .publishers import (PdfResult, PROXY_FALLBACK_PREFIXES, Resolver,
                         WALLED_PREFIXES, proxy_page_resolve)
from .records import Record, load_csv, title_similarity


class RecordTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise RecordTimeout("per-record wall clock cap")


class Downloader:
    def __init__(self, cfg: Config, browser: Browser | None = None):
        self.cfg = cfg
        self.browser = browser
        self.resolver = Resolver(browser, timeout_ms=cfg.timeout_ms * 3) if browser else None
        self.state: dict[str, dict] = {}
        self._load_state()
        self.records: list[Record] = []
        self._last_epmc = 0.0
        self._sd_next_at = 0.0

    # -- state ------------------------------------------------------------
    def _load_state(self) -> None:
        state_files = sorted(self.cfg.out_dir.glob("state*.jsonl"))
        for sf in state_files:
            for line in sf.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("key")
                if not key:
                    continue
                prev = self.state.get(key)
                # keep the newest observation (ISO ts sorts lexicographically)
                if prev is None or (row.get("ts") or "") >= (prev.get("ts") or ""):
                    self.state[key] = row

    def _append_state(self, row: dict) -> None:
        with self.cfg.state_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.state[row["key"]] = row

    def done(self, rec: Record) -> bool:
        row = self.state.get(rec.key)
        if not row:
            return False
        if row.get("status") != "ok":
            return False
        p = Path(row.get("path", ""))
        return p.exists() and p.stat().st_size > self.cfg.min_pdf_bytes

    # -- validation --------------------------------------------------------
    def validate(self, rec: Record, data: bytes) -> tuple[bool, dict]:
        info: dict = {"bytes": len(data)}
        if not data or data[:4] != b"%PDF":
            return False, {**info, "reason": "not a pdf"}
        if len(data) < self.cfg.min_pdf_bytes:
            return False, {**info, "reason": "pdf too small"}
        pages = 0
        sim = None
        warnings.filterwarnings("ignore")
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
            first = (reader.pages[0].extract_text() or "")[:2000]
            sim = round(title_similarity(rec.title, first), 3)
        except Exception:
            pass
        info["pages"] = pages
        info["title_similarity"] = sim
        if pages < 1:
            return False, {**info, "reason": "no pages"}
        return True, info

    # -- storage -----------------------------------------------------------
    def save(self, rec: Record, data: bytes) -> Path:
        path = self.cfg.out_dir / "pdf" / rec.filename()
        path.write_bytes(data)
        return path

    # -- one record --------------------------------------------------------
    def _pmc_page_pdf(self, rec: Record) -> PdfResult | None:
        """PMC article pages expose a direct PDF link; use that for OA copies."""
        pmcid = rec.pmcid.strip()
        if not pmcid:
            return None
        if not pmcid.upper().startswith("PMC"):
            pmcid = "PMC" + pmcid.lstrip("PMCpmc")
        page = self.browser._new_tab()
        try:
            page.goto(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                      wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1200)
            url = page.evaluate(
                """() => { const m = document.querySelector('meta[name="citation_pdf_url"]');
                           return m ? m.content : null; }"""
            )
            if not url:
                return None
            data = fetch_pdf(url, referer=page.url, browser=self.browser,
                             timeout_ms=self.cfg.timeout_ms * 2, browser_fallback=False)
            if data:
                return PdfResult(data=data, source="pmc:oa", status="ok")
        except Exception:
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass
        return None

    def _oa_phase(self, rec: Record) -> PdfResult | None:
        # Europe PMC renders the OA full text of PMC deposits; the endpoint
        # rate-limits hard, so one polite attempt per record.
        if rec.pmcid:
            gap = 8.0 - (time.time() - self._last_epmc)
            if gap > 0:
                time.sleep(gap)
            self._last_epmc = time.time()
            url = sources.epmc_pdf_url(rec.pmcid)
            data = fetch_pdf(url, referer="https://europepmc.org/",
                             browser=None, timeout_ms=90_000, browser_fallback=False)
            if data:
                return PdfResult(data=data, source="europepmc:oa", status="ok")
        if rec.pmcid and self.cfg.use_pmc:
            res = self._pmc_page_pdf(rec)
            if res:
                return res
        for cand in sources.unpaywall(rec.doi, self.cfg.unpaywall_email)[:4]:
            data = fetch_pdf(cand.url, referer=cand.referer, browser=None,
                             timeout_ms=30_000, browser_fallback=False)
            if data and data[:4] == b"%PDF":
                return PdfResult(data=data, source=cand.source, status="ok")
        return None

    def process_one(self, rec: Record) -> dict:
        t0 = time.time()
        result: PdfResult | None = None
        walled = rec.prefix in WALLED_PREFIXES and not self.cfg.attempt_walled
        # 0) --oa-first: repositories before the publisher (fast for PMCIDs)
        if self.cfg.oa_first and (rec.pmcid or rec.doi):
            result = self._oa_phase(rec)
            if result and result.data:
                time.sleep(random.uniform(0.2, 0.8))
        # ScienceDirect rate-limits PDF delivery (HTTP 429): pace requests and
        # back off for several minutes when we trip the limit.
        if rec.prefix == "10.1016":
            wait = self._sd_next_at - time.time()
            if wait > 0:
                time.sleep(min(wait, 420))
        # 1) for sites the campus route cannot reach, try the VPN path first
        if rec.prefix in PROXY_FALLBACK_PREFIXES and self.cfg.proxy_url:
            result = proxy_page_resolve(rec, self.cfg.proxy_url)
            if result and result.data:
                time.sleep(random.uniform(0.3, 1.0))
        # 1b) publisher path: usually the subscribed copy.
        #    Walled publishers are skipped, but their open-access copies are
        #    still worth trying (Europe PMC / repositories).
        if not result and not walled and rec.doi and rec.prefix not in PROXY_FALLBACK_PREFIXES:
            result = self.resolver.resolve(rec)
            time.sleep(random.uniform(*self.cfg.delay_range))
        # 2) open access / repository fallback (also covers walled publishers)
        if not result and (rec.pmcid or rec.doi):
            result = self._oa_phase(rec)
            if result and result.data:
                time.sleep(random.uniform(0.2, 0.8))
        # 3) sites unreachable from the campus route: retry via the user's VPN
        if not result and rec.prefix in PROXY_FALLBACK_PREFIXES and self.cfg.proxy_url:
            result = proxy_page_resolve(rec, self.cfg.proxy_url)
            if result and result.data:
                time.sleep(random.uniform(0.3, 1.0))
        if rec.prefix == "10.1016":
            if result and result.status == "ok":
                self._sd_next_at = time.time() + self.cfg.sd_pace
            elif result and result.status == "rate_limited":
                self._sd_next_at = time.time() + 420
        if not result and walled:
            row = {
                "key": rec.key, "pmid": rec.pmid, "doi": rec.doi, "pmcid": rec.pmcid,
                "title": rec.title, "journal": rec.journal, "year": rec.year,
                "first_author": rec.first_author, "status": "blocked_bot",
                "detail": f"{rec.prefix} sits behind an anti-bot wall (no OA copy)",
                "seconds": round(time.time() - t0, 1),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._append_state(row)
            return row

        if result and result.data:
            ok, info = self.validate(rec, result.data)
            if ok:
                path = self.save(rec, result.data)
                sha = hashlib.sha256(result.data).hexdigest()
                row = {
                    "key": rec.key, "pmid": rec.pmid, "doi": rec.doi, "pmcid": rec.pmcid,
                    "title": rec.title, "journal": rec.journal, "year": rec.year,
                    "first_author": rec.first_author, "status": "ok",
                    "source": result.source, "path": str(path), "sha256": sha,
                    "seconds": round(time.time() - t0, 1),
                    **info,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                self._append_state(row)
                return row
            detail = f"validation failed: {info.get('reason')}"
            status = "invalid_pdf"
        elif result:
            status, detail = result.status, result.detail
        else:
            status, detail = "no_pdf", "no candidate"

        row = {
            "key": rec.key, "pmid": rec.pmid, "doi": rec.doi, "pmcid": rec.pmcid,
            "title": rec.title, "journal": rec.journal, "year": rec.year,
            "first_author": rec.first_author, "status": status, "detail": detail,
            "seconds": round(time.time() - t0, 1),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._append_state(row)
        return row

    # -- batch -------------------------------------------------------------
    def run(self, records: list[Record], limit: int | None = None, retry_failed: bool = False,
            log=print) -> dict:
        todo = []
        for rec in records:
            if self.done(rec):
                continue
            prev = self.state.get(rec.key)
            if prev and prev.get("status") != "ok" and not retry_failed:
                continue
            todo.append(rec)
        if limit:
            todo = todo[:limit]
        counts: dict[str, int] = {}
        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _alarm_handler)
        cap = max(120, self.cfg.timeout_ms // 1000 * 4)
        for i, rec in enumerate(todo, 1):
            signal.setitimer(signal.ITIMER_REAL, cap)
            try:
                row = self.process_one(rec)
            except RecordTimeout:
                try:
                    self.browser.reset()
                except Exception:
                    pass
                row = {"key": rec.key, "pmid": rec.pmid, "doi": rec.doi,
                       "status": "error", "detail": f"record timeout after {cap}s",
                       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                self._append_state(row)
            except Exception as exc:  # keep the batch alive
                detail = repr(exc)[:300]
                if "TargetClosed" in detail or "Target page" in detail or "crash" in detail:
                    try:
                        self.browser.restart()
                    except Exception as exc2:
                        detail += f" | restart failed: {repr(exc2)[:120]}"
                row = {"key": rec.key, "pmid": rec.pmid, "doi": rec.doi,
                       "status": "error", "detail": detail,
                       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                self._append_state(row)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            log(f"[{i}/{len(todo)}] {row['status']:12s} {rec.pmid or ''} {rec.doi} "
                f"({row.get('source', row.get('detail', ''))})")
        signal.signal(signal.SIGALRM, old_handler)
        return counts

    # -- reporting ---------------------------------------------------------
    def export_index(self) -> None:
        rows = [r for r in self.state.values() if r.get("status") == "ok"]
        rows.sort(key=lambda r: (r.get("year", ""), r.get("first_author", "")))
        with self.cfg.index_csv.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["PMID", "DOI", "Title", "Journal", "Year", "FirstAuthor",
                        "Source", "Pages", "Bytes", "File", "SHA256"])
            for r in rows:
                w.writerow([r.get("pmid", ""), r.get("doi", ""), r.get("title", ""),
                            r.get("journal", ""), r.get("year", ""), r.get("first_author", ""),
                            r.get("source", ""), r.get("pages", ""), r.get("bytes", ""),
                            Path(r.get("path", "")).name, r.get("sha256", "")])

    def export_bibtex(self) -> None:
        rows = [r for r in self.state.values() if r.get("status") == "ok"]
        with self.cfg.bib_path.open("w", encoding="utf-8") as fh:
            for r in rows:
                key = f"{_bib_author(r.get('first_author',''))}{r.get('year','')}{r.get('pmid','')}"
                fh.write(f"@article{{{key},\n")
                fh.write(f"  title   = {{{_tex(r.get('title',''))}}},\n")
                fh.write(f"  author  = {{{_tex(r.get('first_author',''))}}},\n")
                fh.write(f"  journal = {{{_tex(r.get('journal',''))}}},\n")
                fh.write(f"  year    = {{{r.get('year','')}}},\n")
                if r.get("doi"):
                    fh.write(f"  doi     = {{{r['doi']}}},\n")
                if r.get("pmid"):
                    fh.write(f"  pmid    = {{{r['pmid']}}},\n")
                fh.write("}\n\n")

    def reconcile_disk(self, csv_path: Path) -> int:
        """Add PDFs present on disk but absent from state (duplicate DOIs etc.)."""
        from .records import load_csv
        by_pmid = {r.pmid: r for r in load_csv(csv_path)}
        ok_paths = {Path(r.get("path", "")).name for r in self.state.values()
                    if r.get("status") == "ok"}
        added = 0
        for f in sorted((self.cfg.out_dir / "pdf").glob("*.pdf")):
            if f.name in ok_paths:
                continue
            pmid = f.stem.rsplit("_", 1)[-1]
            rec = by_pmid.get(pmid)
            if rec is None:
                continue
            self.state["disk:" + f.name] = {
                "key": "disk:" + f.name, "pmid": rec.pmid, "doi": rec.doi,
                "title": rec.title, "journal": rec.journal, "year": rec.year,
                "first_author": rec.first_author, "status": "ok",
                "source": "disk-orphan", "path": str(f),
                "bytes": f.stat().st_size,
            }
            added += 1
        return added

    def export_missing(self) -> None:
        rows = [r for r in self.state.values() if r.get("status") != "ok"]
        rows.sort(key=lambda r: (r.get("status", ""), r.get("doi", "")))
        path = self.cfg.out_dir / "missing.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["PMID", "DOI", "PMCID", "Title", "Journal", "Year",
                        "Status", "Detail"])
            for r in rows:
                w.writerow([r.get("pmid", ""), r.get("doi", ""), r.get("pmcid", ""),
                            r.get("title", ""), r.get("journal", ""), r.get("year", ""),
                            r.get("status", ""), r.get("detail", "")])

    def summary_markdown(self) -> str:
        st = self.stats()
        total = sum(st["counts"].values())
        ok = st["counts"].get("ok", 0)
        lines = ["# 下载报告", "",
                 f"- 记录总数：{total}",
                 f"- 成功下载：{ok}（{100.0*ok/total:.1f}%）" if total else "",
                 ""]
        lines.append("| 状态 | 数量 |")
        lines.append("| --- | --- |")
        for k, v in sorted(st["counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {v} |")
        lines += ["", "| DOI 前缀 | 成功/总数 | 其他状态 |", "| --- | --- | --- |"]
        for pref, d in sorted(st["per_prefix"].items(), key=lambda kv: -sum(kv[1].values())):
            tot = sum(d.values()); good = d.get("ok", 0)
            other = ", ".join(f"{k}:{v}" for k, v in d.items() if k != "ok")
            lines.append(f"| {pref or '(none)'} | {good}/{tot} | {other} |")
        return "\n".join(lines) + "\n"

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        per_prefix: dict[str, dict[str, int]] = {}
        for r in self.state.values():
            st = r.get("status", "?")
            counts[st] = counts.get(st, 0) + 1
            doi = r.get("doi", "")
            pref = doi.split("/")[0] if "/" in doi else "(none)"
            per_prefix.setdefault(pref, {})
            per_prefix[pref][st] = per_prefix[pref].get(st, 0) + 1
        return {"counts": counts, "per_prefix": per_prefix}


def _bib_author(a: str) -> str:
    a = (a or "anon").split()[0]
    return "".join(ch for ch in a if ch.isalnum()) or "anon"


def _tex(s: str) -> str:
    return (s or "").replace("&", r"\&").replace("%", r"\%").replace("_", r"\_").replace("#", r"\#")
