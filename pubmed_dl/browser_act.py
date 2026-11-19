"""browser-act backend for challenge-fronted publisher pages.

Why this exists
---------------
Cloudflare's managed challenge does not clear for browser-act's local ``chrome``
browser: it runs with automation flags that the challenge keeps rejecting, even
from the campus egress IP.  The project's headed Chrome (``pubmed_dl.browser``)
does clear it, so this backend seeds the resulting clearance cookies into
browser-act once per domain and then uses browser-act for the rest:

    navigate -> wait for the article -> citation_pdf_url -> cookies export
             -> plain HTTP fetch of the PDF bytes

The cookie export is the important part: bare curl has no clearance cookie and
every PDF endpoint answers 403 to it.

browser-act itself must reach the publisher from the campus IP.  Its Chrome
inherits the desktop proxy, so the daemon has to be started with the proxy
environment removed and GSettings mocked:

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \\
        -u ALL_PROXY -u all_proxy GSETTINGS_BACKEND=memory \\
        browser-act daemon restart

``BrowserAct.available`` only checks that the CLI and the named browser exist;
a wrong egress IP surfaces as a challenge that never clears and the caller
falls back to the Playwright path.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .browser import BOT_WALL_MARKERS
from .fetch import UA, _clean_env

CLI = "browser-act"
DEFAULT_BROWSER = "pubmed-publishers"

_PDF_JS = """(() => {
  const out = [];
  const m = document.querySelector('meta[name="citation_pdf_url"]');
  if (m && m.content) out.push(m.content);
  const l = document.querySelector('link[type="application/pdf"]');
  if (l && l.href) out.push(l.href);
  for (const a of document.querySelectorAll('a[href]')) {
    const u = a.href;
    if (/pdfdirect|pdfft|articlepdf|\\/pdf\\//i.test(u) || /\\.pdf(\\?|$)/i.test(u)) out.push(u);
  }
  return JSON.stringify([...new Set(out)].slice(0, 6));
})()"""

_TITLE_JS = "document.title"
_UA_JS = "navigator.userAgent"


class BrowserActError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return (text or "").strip()


class BrowserAct:
    """Thin driver over the ``browser-act`` CLI (one named session)."""

    def __init__(self, browser=None, name: str = DEFAULT_BROWSER,
                 session: str = "pubmed-pub", wall_wait_s: float = 60.0,
                 timeout_s: float = 180.0, display: str = ":99"):
        self.browser = browser          # pubmed_dl.browser.Browser, used to seed cookies
        self.name = name
        self.session = session
        self.wall_wait_s = wall_wait_s
        self.timeout_s = timeout_s
        self.display = display
        self.browser_id: str | None = None
        self._opened = False
        self._ua: str | None = None

    # -- discovery ---------------------------------------------------------
    def available(self) -> bool:
        try:
            out = subprocess.run([CLI, "browser", "list"], capture_output=True,
                                 text=True, timeout=30, env=_clean_env())
        except (FileNotFoundError, subprocess.SubprocessError):
            return False
        if out.returncode != 0:
            return False
        for line in out.stdout.splitlines():
            if f'name="{self.name}"' in line and line.startswith("id="):
                self.browser_id = line.split()[0][3:]
                return bool(self.browser_id)
        return False

    # -- plumbing ----------------------------------------------------------
    def _env(self) -> dict:
        env = _clean_env()
        if self.display:
            env["DISPLAY"] = self.display
        return env

    def _run(self, args: list[str], timeout: float | None = None, check: bool = True) -> str:
        cmd = [CLI, "--session", self.session, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout or self.timeout_s, env=self._env())
        except subprocess.TimeoutExpired as exc:
            raise BrowserActError(f"browser-act timed out: {' '.join(args)}") from exc
        out = _clean(proc.stdout)
        if check and proc.returncode != 0:
            raise BrowserActError((out + "\n" + _clean(proc.stderr))[:500])
        return out

    def _eval(self, js: str) -> str:
        out = self._run(["eval", js])
        if out in ("(no return value)", "undefined", "null"):
            return ""
        return out

    @property
    def ua(self) -> str:
        if not self._ua:
            try:
                self._ua = self._eval(_UA_JS) or UA
            except BrowserActError:
                self._ua = UA
        return self._ua

    # -- session -----------------------------------------------------------
    def ensure(self) -> bool:
        if self._opened:
            return True
        if not self.browser_id and not self.available():
            return False
        if not self.browser_id:
            return False
        # Parallel shards share one browser-act browser; serialise the first
        # open so they cannot race a headless/headed relaunch.
        lock = Path(tempfile.gettempdir()) / "pubmed_dl_browser_act.lock"
        with lock.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            out = self._run(["browser", "open", self.browser_id, "about:blank", "--headed"],
                            check=False)
            if "running in headless mode" in out.lower():
                # A headless Chrome never clears the challenge; replace it with
                # a headed one on the project's Xvfb display.
                self._run(["session", "close", self.session], timeout=30, check=False)
                subprocess.run(["pkill", "-f", f"browseract/profiles/{self.browser_id}"],
                               capture_output=True)
                time.sleep(2)
                out = self._run(["browser", "open", self.browser_id, "about:blank", "--headed"],
                                check=False)
        if "Error" in out and "already open" not in out.lower():
            return False
        self._opened = True
        return True

    def close(self) -> None:
        if not self._opened:
            return
        self._run(["session", "close", self.session], timeout=30, check=False)
        self._opened = False

    # -- page --------------------------------------------------------------
    def navigate(self, url: str) -> None:
        self._run(["navigate", url], check=False)

    def title(self) -> str:
        try:
            return self._eval(_TITLE_JS)
        except BrowserActError:
            return ""

    def challenge_present(self) -> bool:
        title = self.title().lower()
        if any(m in title for m in BOT_WALL_MARKERS):
            return True
        return False

    def wait_article(self, seconds: float | None = None) -> bool:
        """Poll until the challenge is gone *and* the article has rendered.

        "Not a challenge" alone is not readiness: JAMA (for one) returns an
        empty document for several seconds before the article renders, and
        treating that as ready produced a bogus no_pdf verdict.
        """
        deadline = time.time() + (seconds if seconds is not None else self.wall_wait_s)
        while time.time() < deadline:
            if not self.challenge_present():
                if self.pdf_candidates():
                    return True
                if self.title():
                    try:
                        size = int(self._eval("document.body ? document.body.innerText.length : 0") or 0)
                    except (BrowserActError, ValueError):
                        size = 0
                    if size > 200:
                        return True
            time.sleep(2)
        return False

    def current_url(self) -> str:
        try:
            return self._eval("location.href")
        except BrowserActError:
            return ""

    def pdf_candidates(self) -> list[str]:
        """PDF links in preference order (citation meta first, then anchors)."""
        try:
            raw = self._eval(_PDF_JS)
        except BrowserActError:
            return []
        try:
            values = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
        return [u for u in values if isinstance(u, str) and u.startswith("http")]

    # -- cookies -----------------------------------------------------------
    def seed_cookies(self, landing: str) -> bool:
        """Copy fresh clearance cookies from the Playwright Chrome into browser-act.

        Two details matter and were measured:
          * ``expires`` must be an integer - the CLI silently drops cookies
            whose expiry it cannot parse, which left ``cf_clearance`` behind;
          * the import must happen on a benign same-origin URL (robots.txt)
            after clearing the domain, otherwise the challenge response
            installs a new, non-authorising ``cf_clearance`` first.
        """
        if self.browser is None:
            return False
        page = self.browser._new_tab()
        try:
            try:
                page.goto(landing, wait_until="commit", timeout=45_000)
            except Exception:
                pass
            if not self.browser.wait_past_wall(page, self.wall_wait_s):
                return False
            cookies = self.browser.context.cookies([page.url])
            host = urlparse(page.url).hostname or urlparse(landing).hostname
            if not cookies or not host:
                return False
            rows = []
            for c in cookies:
                expires = c.get("expires", -1)
                rows.append({
                    "name": c["name"], "value": c["value"],
                    "domain": c["domain"], "path": c["path"],
                    "expires": int(expires) if isinstance(expires, (int, float)) and expires > 0 else -1,
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "sameSite": c.get("sameSite") if c.get("sameSite") in ("Lax", "Strict", "None") else "Lax",
                })
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(rows, fh)
                path = fh.name
            try:
                self.navigate(f"https://{host}/robots.txt")
                self._run(["cookies", "clear", "--url", f"https://{host}/", "--force"], check=False)
                self._run(["cookies", "import", path], check=False)
            finally:
                Path(path).unlink(missing_ok=True)
            return True
        finally:
            try:
                page.close()
            except Exception:
                pass

    def export_cookies(self, url: str) -> list[dict]:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            path = fh.name
        try:
            self._run(["cookies", "export", path, "--url", url])
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (BrowserActError, json.JSONDecodeError):
            return []
        finally:
            Path(path).unlink(missing_ok=True)

    # -- pdf ---------------------------------------------------------------
    def fetch_pdf(self, pdf_url: str, referer: str) -> bytes | None:
        cookies = self.export_cookies(pdf_url)
        session = requests.Session()
        session.trust_env = False
        for c in cookies:
            try:
                session.cookies.set(c["name"], c["value"],
                                    domain=c.get("domain"), path=c.get("path", "/"))
            except Exception:
                pass
        try:
            resp = session.get(
                pdf_url,
                headers={"User-Agent": self.ua, "Accept": "application/pdf,*/*",
                         "Referer": referer or pdf_url},
                timeout=min(self.timeout_s, 180), allow_redirects=True,
            )
            body = resp.content
            if body[:4] == b"%PDF":
                return body
        except Exception:
            return None
        return None

    # -- one record --------------------------------------------------------
    def resolve(self, doi: str) -> tuple[bytes | None, str, str]:
        """Return (pdf bytes, source, detail) for one DOI."""
        if not doi:
            return None, "browser-act", "no DOI"
        landing = f"https://doi.org/{doi}"
        try:
            if not self.ensure():
                return None, "browser-act", f"no browser named {self.name!r}"
            # Try the cached clearance first: once a domain is seeded the
            # cookie survives for hours, and re-seeding costs a Playwright page
            # load per record.  Only pay that when the page is challenged.
            self.navigate(landing)
            if not self.wait_article(15):
                if not self.seed_cookies(self.current_url() or landing):
                    return None, "browser-act", "challenge did not clear"
                self.navigate(landing)
                if not self.wait_article():
                    return None, "browser-act", "challenge survived cookie seed"
            candidates = self.pdf_candidates()
            for _ in range(4):
                if candidates:
                    break
                time.sleep(2)          # link may be rendered after the article
                candidates = self.pdf_candidates()
            if not candidates:
                return None, "browser-act", "no pdf link on article"
            referer = self.current_url() or landing
            for pdf in candidates:
                data = self.fetch_pdf(pdf, referer)
                if data:
                    host = urlparse(pdf).hostname or "?"
                    return data, f"browser-act:{host}", ""
            return None, "browser-act", "all pdf candidates failed"
        except BrowserActError as exc:
            return None, "browser-act", str(exc)[:200]
