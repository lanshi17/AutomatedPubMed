"""Publisher adapters.

Strategy per record:
  1. a known direct PDF endpoint for the DOI prefix (cheap, no page render)
  2. generic article-page resolution: follow https://doi.org/<doi>, read the
     standard `citation_pdf_url` meta tag and/or PDF links, then fetch
  3. if the page is an anti-bot wall, wait briefly; if it does not clear we
     report `blocked_bot` instead of trying to defeat it.

`Resolver` also caches "this prefix is walled" verdicts so a bulk run does not
spend 20 s per article on a publisher that blocks every request.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .browser import Browser
from .fetch import fetch_pdf
from .records import Record

# DOI prefix -> (kind, url template)
DIRECT: dict[str, tuple[str, str]] = {
    # Springer Nature Link (includes BMC / SpringerOpen after platform merge)
    "10.1007": ("request", "https://link.springer.com/content/pdf/{doi}.pdf"),
    "10.1186": ("request", "https://link.springer.com/content/pdf/{doi}.pdf"),
    "10.1182": ("request", "https://ashpublications.org/blood/article-pdf/doi/{doi}"),
    # Nature Portfolio
    "10.1038": ("request", "https://www.nature.com/articles/{suffix}.pdf"),
    # Taylor & Francis
    "10.1080": ("page", "https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true"),
    "10.3109": ("page", "https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true"),
    # SAGE
    "10.1177": ("page", "https://journals.sagepub.com/doi/pdf/{doi}"),
    # RSNA
    "10.1148": ("page", "https://pubs.rsna.org/doi/pdf/{doi}"),
    # Frontiers handled by _frontiers_pdf (needs journal slug)
    # Hindawi content now lives on Wiley Online Library
    "10.1155": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    # Wiley
    "10.1002": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    "10.1111": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    "10.1046": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    "10.7863": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    "10.1111": ("download", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"),
    # PLOS
    "10.1371": ("request", "https://journals.plos.org/plosone/article/file?id={doi}&type=printable"),
}

WILEY_PREFIXES = {"10.1002", "10.1111", "10.1046", "10.7863", "10.1111"}

# Prefixes with an interactive check (not a managed challenge) or no working
# route at all.  These are skipped unless --attempt-walled.  Cloudflare managed
# challenges (JAMA, NEJM, ...) are not listed: they clear by themselves and the
# resolver waits them out.
WALLED_PREFIXES = {
    "10.1126",  # Science/AAAS (Cloudflare; not re-verified)
    "10.3760",  # Chinese Medical Association / Yiigle (captcha on PDF)
    "10.1055",  # Thieme (very slow / connection failures)
}

# Registry domain per browser-act prefix, used to check whether its cookie
# jar already holds a clearance cookie.
ACT_DOMAINS = {
    "10.1001": "jamanetwork.com",
    "10.1056": "nejm.org",
    "10.1080": "tandfonline.com",
    "10.3109": "tandfonline.com",
    "10.1093": "oup.com",
    "10.1136": "bmj.com",
    "10.1159": "karger.com",
    "10.1177": "sagepub.com",
    "10.1148": "rsna.org",
    "10.1126": "science.org",
}

# Hosts whose article pages sit behind a JS challenge (Cloudflare managed
# challenge on the campus route).  Their PDF endpoints need the clearance
# cookie, so the browser context must carry the request, not bare curl.
# DOI prefixes resolved through browser-act first (see browser_act.py).
ACT_PREFIXES = frozenset({
    "10.1001",  # JAMA
    "10.1056",  # NEJM
    "10.1080",  # T&F
    "10.3109",  # T&F (legacy)
    "10.1093",  # OUP
    "10.1136",  # BMJ
    "10.1159",  # Karger
    "10.1177",  # SAGE
    "10.1148",  # RSNA
    "10.1126",  # Science
})

CHALLENGE_FRONTED_HOSTS = (
    "karger.com",
    "tandfonline.com",
    "sagepub.com",
    "rsna.org",
    "oup.com",
    "bmj.com",
    "jamanetwork.com",
    "nejm.org",
    "science.org",
    "yiigle.com",
)


def _challenge_fronted(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in CHALLENGE_FRONTED_HOSTS)


# Sites that are unreachable from the campus route; we retry them through the
# user's own VPN (connectivity fallback for open-access content, not an
# access-control bypass).
PROXY_FALLBACK_PREFIXES = {"10.3892", "10.12659", "10.5603", "10.1590", "10.3346",
                            "10.17772", "10.1507", "10.4238"}

ENTITLED_MARKERS = ("full access", "access through your institution", "access via",
                    "download pdf", "view pdf", "open access", "free access",
                    "download full", "pdf download")
PAYWALL_MARKERS = ("get access", "buy article", "purchase pdf", "rent this article",
                   "buy now", "subscribe to", "sign in to download", "access options",
                   "purchase article")

# Hosts that only mirror/redirect landing pages and never serve the PDF.
DEAD_HOSTS = ("cnpereading.com", "oce.ovid.com", "kglmeridian.com", "yiigle.com")


@dataclass
class PdfResult:
    data: bytes | None
    source: str
    status: str          # ok | blocked_bot | not_entitled | no_pdf | error
    detail: str = ""


def _suffix(doi: str) -> str:
    return doi.split("/", 1)[1] if "/" in doi else ""


def _crossref_url(doi: str, timeout: float = 12.0) -> str | None:
    """Ask Crossref for the publisher landing URL (avoids a flaky doi.org hop)."""
    import requests

    try:
        r = requests.get(
            f"https://api.crossref.org/works/{doi}", timeout=timeout,
            headers={"User-Agent": "pubmed_dl/1.0 (mailto:yangzs@csu.edu.cn)"},
        )
        if r.status_code != 200:
            return None
        msg = (r.json() or {}).get("message") or {}
        url = ((msg.get("resource") or {}).get("primary") or {}).get("URL")
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception:
        return None


def proxy_page_resolve(rec: Record, proxy: str, timeout_ms: int = 120_000) -> PdfResult:
    """Resolve a PDF via the user's VPN when the campus route cannot reach the site."""
    import html as htmllib
    import subprocess
    from .fetch import UA, _clean_env, curl_pdf

    landing = f"https://doi.org/{rec.doi}"
    try:
        out = subprocess.run(
            ["curl", "-sL", "--proxy", proxy, "--compressed", "-m", "45", "-A", UA,
             "-w", "\n%{url_effective}", landing],
            env=_clean_env(), capture_output=True, text=True, timeout=60,
        )
        text = out.stdout or ""
    except Exception:
        return PdfResult(None, "proxy", "error", "proxy fetch failed")

    m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', text, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url', text, re.I)
    url = htmllib.unescape(m.group(1)) if m else None
    if not url:
        m2 = re.search(r'href=["\']([^"\']+?\.pdf[^"\']*)', text, re.I)
        url = htmllib.unescape(m2.group(1)) if m2 else None
    if not url:
        return PdfResult(None, "proxy", "no_pdf", "no pdf link on proxied page")
    if url.startswith("//"):
        url = "https:" + url
    data = curl_pdf(url, referer=landing, timeout=90, proxy=proxy)
    if data:
        return PdfResult(data=data, source=f"proxy:{_host(url)}", status="ok")
    return PdfResult(None, "proxy", "no_pdf", "proxied pdf fetch failed")


def _frontiers_pdf_url(doi: str) -> str | None:
    """Resolve the journal-slug path Frontiers needs, then swap /full -> /pdf."""
    import subprocess
    from .fetch import _clean_env, UA
    landing = f"https://www.frontiersin.org/articles/{doi}/full"
    try:
        out = subprocess.run(
            ["curl", "-sL", "--noproxy", "*", "-m", "30", "-A", UA,
             "-o", "/dev/null", "-w", "%{http_code} %{url_effective}", landing],
            env=_clean_env(), capture_output=True, text=True, timeout=45,
        )
        parts = (out.stdout or "").strip().split(" ", 1)
        if len(parts) == 2 and parts[0] == "200" and parts[1].endswith("/full"):
            return parts[1][:-5] + "/pdf"
    except Exception:
        pass
    return None


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else "?"


class Resolver:
    def __init__(self, browser: Browser, wall_wait_s: float = 90.0, timeout_ms: int = 120_000,
                 wall_cooldown_s: float = 180.0, act=None, rounds: int = 2,
                 act_first: bool = False):
        self.browser = browser
        self.wall_wait_s = wall_wait_s
        self.timeout_ms = timeout_ms
        self.wall_cooldown_s = wall_cooldown_s
        self.act = act  # pubmed_dl.browser_act.BrowserAct | None
        self.rounds = max(1, rounds)
        # Prefixes where browser-act already produced a PDF: use it first for
        # the remaining records, since its cookie jar stays warm.
        self.act_first = act_first
        self.act_ok_prefixes: set[str] = set()
        self._warm_checked: set[str] = set()
        # prefix -> [consecutive_walls, cooldown_until_ts]
        self.walled: dict[str, list[float]] = {}

    def _wall_state(self, prefix: str) -> list[float]:
        return self.walled.setdefault(prefix, [0.0, 0.0])

    def _in_cooldown(self, prefix: str) -> bool:
        _n, until = self._wall_state(prefix)
        return time.time() < until

    def _note_wall(self, prefix: str) -> None:
        st = self._wall_state(prefix)
        st[0] += 1
        if st[0] >= 5:
            # A JS challenge normally clears by itself; five in a row means we
            # are being throttled, so pause this publisher briefly instead of
            # burning every remaining record on it.
            st[1] = time.time() + self.wall_cooldown_s
            st[0] = 0

    def _note_ok(self, prefix: str) -> None:
        self.walled[prefix] = [0.0, 0.0]

    # ------------------------------------------------------------------
    def resolve(self, rec: Record) -> PdfResult:
        """Resolve one DOI, preferring completeness over latency.

        Each round runs the cheap direct endpoint, the Playwright page and the
        browser-act fallback.  A second round re-tries transient challenge /
        network verdicts; a real paywall (``not_entitled``) returns at once.
        """
        if not rec.doi:
            return PdfResult(None, "none", "no_pdf", "record has no DOI")
        last: PdfResult | None = None
        rounds = 1 if self._in_cooldown(rec.prefix) else self.rounds
        if self.act_first and rec.prefix not in self._warm_checked:
            self._warm_checked.add(rec.prefix)
            if self._act_warm(rec.prefix):
                self.act_ok_prefixes.add(rec.prefix)
        act_first = self.act_first and rec.prefix in self.act_ok_prefixes
        tried_act = False
        for round_i in range(rounds):
            if act_first and not tried_act:
                tried_act = True
                hit = self._try_act(rec)
                if hit:
                    return hit

            result = self._direct(rec)
            if result and result.status == "ok":
                self._note_ok(rec.prefix)
                return result
            if result and result.status == "not_entitled":
                return result
            last = last or result

            page = self._article_page(rec)
            if page.status == "ok":
                return page
            if page.status == "not_entitled":
                return page
            last = page

            if not tried_act:
                tried_act = True
                hit = self._try_act(rec)
                if hit:
                    return hit

            if round_i + 1 < rounds:
                time.sleep(2.0)
        return last or PdfResult(None, "page", "no_pdf", "no candidate")

    def _act_warm(self, prefix: str) -> bool:
        """True when browser-act already holds a clearance cookie for the domain."""
        if self.act is None or prefix not in ACT_DOMAINS:
            return False
        try:
            if not self.act.ensure():
                return False
            cookies = self.act.export_cookies(f"https://{ACT_DOMAINS[prefix]}/")
        except Exception:
            return False
        return any(c.get("name") == "cf_clearance" for c in cookies)

    def _try_act(self, rec: Record) -> PdfResult | None:
        if self.act is None or rec.prefix not in ACT_PREFIXES:
            return None
        data, source, _detail = self.act.resolve(rec.doi)
        if data:
            self._note_ok(rec.prefix)
            self.act_ok_prefixes.add(rec.prefix)
            return PdfResult(data=data, source=source, status="ok")
        self._note_wall(rec.prefix)
        return None

    # ------------------------------------------------------------------
    def _direct(self, rec: Record) -> PdfResult | None:
        if rec.prefix == "10.1016":
            return self._elsevier(rec)
        if rec.prefix == "10.1159":
            # Karger: article id = DOI suffix without leading zeros
            aid = rec.doi.split("/", 1)[1].lstrip("0") if "/" in rec.doi else ""
            if aid:
                url = f"https://karger.com/Article/Pdf/{aid}"
                data = fetch_pdf(url, referer=f"https://karger.com/doi/{rec.doi}",
                                 browser=self.browser, timeout_ms=self.timeout_ms,
                                 curl_first=False)
                if data:
                    return PdfResult(data=data, source="direct:10.1159", status="ok")
        if rec.prefix == "10.3389":
            url = _frontiers_pdf_url(rec.doi)
            if url:
                data = fetch_pdf(url, referer=url.replace("/pdf", "/full"),
                                 browser=self.browser, timeout_ms=self.timeout_ms)
                if data:
                    return PdfResult(data=data, source="direct:10.3389", status="ok")
        entry = DIRECT.get(rec.prefix)
        if not entry:
            return None
        _, template = entry
        url = template.format(doi=rec.doi, suffix=_suffix(rec.doi))
        referer = "https://onlinelibrary.wiley.com/" if url.startswith("https://onlinelibrary") else None
        data = fetch_pdf(url, referer=referer, browser=self.browser,
                         timeout_ms=self.timeout_ms,
                         curl_first=not _challenge_fronted(url))
        if data:
            return PdfResult(data=data, source=f"direct:{rec.prefix}", status="ok")
        return None

    # ------------------------------------------------------------------
    def _article_page(self, rec: Record) -> PdfResult:
        if self._in_cooldown(rec.prefix):
            return PdfResult(None, "page", "blocked_bot",
                             f"{rec.prefix} cooling down after repeated challenges")

        page = self.browser._new_tab()
        try:
            # Ask Crossref for the publisher landing URL first: it is one cheap
            # API call, it skips the flaky doi.org hop, and it reveals when a
            # journal has migrated to another platform (e.g. T&F -> Wiley).
            targets = [u for u in (_crossref_url(rec.doi),) if u]
            targets.append(f"https://doi.org/{rec.doi}")
            err = ""
            landed = False
            for target in targets:
                for _attempt in (1, 2):
                    try:
                        page.goto(target, wait_until="domcontentloaded", timeout=40_000)
                        landed = True
                        break
                    except Exception as exc:
                        err = repr(exc)[:180]
                        # The challenge document can hold DOMContentLoaded past
                        # the cap and render the article anyway; keep the tab
                        # when it already sits on the requested URL.
                        try:
                            if (page.url or "").startswith("http"):
                                landed = True
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(800)
                if landed:
                    break
            if not landed:
                return PdfResult(None, "page", "error", err)
            page.wait_for_timeout(1500)
            _ = self.browser.text(page, retries=1)

            if self.browser.is_bot_wall(page):
                cleared = self.browser.wait_past_wall(page, self.wall_wait_s)
                if not cleared:
                    self._note_wall(rec.prefix)
                    return PdfResult(None, "page", "blocked_bot",
                                     f"anti-bot wall on {_host(page.url)}")

            body = self.browser.text(page)
            if len(body) < 200 and not self.browser.is_bot_wall(page):
                # A slow article shell can render after DOMContentLoaded; do
                # not declare no_pdf against an empty document.
                for _ in range(10):
                    page.wait_for_timeout(1500)
                    body = self.browser.text(page)
                    if len(body) >= 200:
                        break
            low = body.lower()
            host = _host(page.url)

            # Redirect-only mirrors serve metadata but never a PDF; give up at
            # once instead of burning minutes on their links.
            if any(m in host for m in DEAD_HOSTS):
                return PdfResult(None, "page", "no_pdf", f"{host} is a mirror without PDF")

            # Journals that migrated between platforms keep serving the DOI on
            # the new host; the platform's own direct-download route is then far
            # more reliable than the legacy /doi/pdf/<doi> link on the page.
            if "onlinelibrary.wiley.com" in host:
                data = fetch_pdf(
                    f"https://{host}/doi/pdfdirect/{rec.doi}?download=true",
                    referer=page.url, browser=self.browser, timeout_ms=90_000)
                if data:
                    self._note_ok(rec.prefix)
                    return PdfResult(data=data, source=f"page:{host}:pdfdirect", status="ok")

            pdf_url = self._citation_pdf_url(page)
            links = ([pdf_url] if pdf_url else []) + self.browser.find_pdf_links(page)
            links = [u for u in dict.fromkeys(links) if u and u.startswith("http")]

            paywalled = any(k in low for k in PAYWALL_MARKERS) and not any(
                k in low for k in ENTITLED_MARKERS)
            if paywalled:
                return PdfResult(None, "page", "not_entitled",
                                 f"paywall on {host}")
            if not links:
                return PdfResult(None, "page", "no_pdf",
                                 f"no pdf link on {host} (len={len(body)})")

            referer = page.url
            # Cap each candidate: on a walled/paywalled host the first link can
            # stall for minutes, and one slow article must not eat the batch.
            link_timeout = min(self.timeout_ms, 50_000)
            for url in links[:3]:
                data = fetch_pdf(url, referer=referer, browser=self.browser,
                                 timeout_ms=link_timeout, curl_first=False)
                if data:
                    self._note_ok(rec.prefix)
                    return PdfResult(data=data, source=f"page:{_host(url)}", status="ok")
                if "yiigle" in url:
                    return PdfResult(None, "page", "blocked_bot",
                                     "yiigle CAPTCHA on PDF endpoint")
            return PdfResult(None, "page", "no_pdf", f"tried {len(links)} pdf links")
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _elsevier(self, rec: Record) -> PdfResult:
        """ScienceDirect: the PDF lives behind a tokenised asset URL, so we
        intercept the response body through CDP instead of downloading it."""
        if self._in_cooldown("10.1016"):
            return PdfResult(None, "elsevier", "blocked_bot",
                             "ScienceDirect cooling down after repeated challenges")
        page = self.browser._new_tab()
        try:
            try:
                page.goto(f"https://doi.org/{rec.doi}", wait_until="domcontentloaded",
                          timeout=45_000)
            except Exception as exc:
                return PdfResult(None, "elsevier", "error", repr(exc)[:150])
            page.wait_for_timeout(1200)
            if self.browser.is_bot_wall(page) and not self.browser.wait_past_wall(page, 20):
                self._note_wall("10.1016")
                return PdfResult(None, "elsevier", "blocked_bot", "ScienceDirect challenge")
            body = self.browser.text(page, retries=1)
            low = body.lower()
            links = [u for u in self.browser.find_pdf_links(page) if "/pdfft" in u]
            if not links:
                if "purchase pdf" in low or "get access" in low or "buy" in low:
                    return PdfResult(None, "elsevier", "not_entitled",
                                     "SD paywall shown (no subscription)")
                return PdfResult(None, "elsevier", "no_pdf", "no pdfft link on SD page")
            data = self.browser.fetch_via_cdp(page, links[0])
            status = getattr(self.browser, "last_fetch_status", None)
            if data:
                return PdfResult(data=data, source="elsevier:cdp", status="ok")
            if status == 429:
                return PdfResult(None, "elsevier", "rate_limited", "HTTP 429 from SD pdfft")
            return PdfResult(None, "elsevier", "no_pdf", f"asset capture failed (status={status})")
        finally:
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _citation_pdf_url(page) -> str | None:
        try:
            return page.evaluate(
                """() => { const m = document.querySelector('meta[name="citation_pdf_url"]');
                           if (m && m.content) return m.content;
                           const l = document.querySelector('link[type="application/pdf"]');
                           return l ? l.href : null; }"""
            )
        except Exception:
            return None


def resolve(rec: Record, browser: Browser, *, timeout_ms: int = 120_000,
            wall_wait_s: float = 90.0, act=None, rounds: int = 2,
            wall_cooldown_s: float = 180.0, act_first: bool = False) -> PdfResult:
    """One-shot resolution without the per-prefix wall cache of `Resolver`."""
    return Resolver(browser, wall_wait_s=wall_wait_s, timeout_ms=timeout_ms,
                    wall_cooldown_s=wall_cooldown_s, act=act,
                    rounds=rounds, act_first=act_first).resolve(rec)
