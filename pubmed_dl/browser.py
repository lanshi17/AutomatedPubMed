"""Chrome session management over the DevTools protocol.

The browser matters here for two reasons:
  1. Publisher entitlement is IP based (campus network), so we must NOT go
     through the machine's local proxy (127.0.0.1:7890).
  2. Several publishers gate content behind JS challenges; a real Chrome that
     passes them normally does fine, while raw HTTP requests do not.

We launch the *system* Chrome (not Playwright's bundled build) so that its
TLS/JS fingerprint is the ordinary one, and talk to it over CDP.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

BOT_WALL_MARKERS = (
    "just a moment",
    "请稍候",
    "attention required",
    "are you a robot",
    "performing security verification",
    "verify you are human",
    "checking your browser",
    "captcha",
    "access denied",
)

CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    raise RuntimeError("no Chrome/Chromium binary found")


def _cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_display(display: str) -> None:
    if not display or not display.startswith(":"):
        return
    num = display[1:].split(".")[0]
    if Path(f"/tmp/.X11-unix/X{num}").exists():
        return
    if shutil.which("Xvfb") is None:
        raise RuntimeError(f"display {display} missing and Xvfb not installed")
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1600x1000x24"],
        stdout=open("/tmp/pubmed_dl_xvfb.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(1.5)


def launch_chrome(port: int, profile: Path, display: str) -> None:
    chrome = find_chrome()
    profile.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("all_proxy", None)
    env.pop("ALL_PROXY", None)
    if display:
        env["DISPLAY"] = display
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-proxy-server",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-features=Translate",
        "--window-size=1400,960",
        "about:blank",
    ]
    subprocess.Popen(
        args,
        env=env,
        stdout=open("/tmp/pubmed_dl_chrome.log", "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(50):
        if _cdp_alive(port):
            return
        time.sleep(0.3)
    raise RuntimeError(f"Chrome CDP did not come up on port {port}")


class Browser:
    """Thin wrapper around a CDP-connected Chrome."""

    def __init__(self, port: int = 9222, display: str = ":99",
                 profile: Path = Path("/tmp/pubmed_dl_chrome"), timeout_ms: int = 60_000):
        self.port = port
        self.display = display
        self.profile = Path(profile)
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page = None
        self.user_agent = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "Browser":
        if not _cdp_alive(self.port):
            ensure_display(self.display)
            launch_chrome(self.port, self.profile, self.display)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        self._ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        try:
            self.user_agent = self.page.evaluate("() => navigator.userAgent")
        except Exception:
            self.user_agent = ""
        return self

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._browser = self._ctx = self._pw = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- helpers -----------------------------------------------------------
    @property
    def context(self):
        return self._ctx

    def restart(self) -> None:
        """Recover from a crashed renderer/browser."""
        self.close()
        subprocess.run(["fuser", "-k", f"{self.port}/tcp"], capture_output=True)
        time.sleep(1.0)
        shutil.rmtree(self.profile, ignore_errors=True)
        self.start()

    def reset(self) -> None:
        """Close stray tabs so a timed-out record cannot wedge the session."""
        try:
            pages = list(self._ctx.pages)
            for p in pages[1:]:
                try:
                    p.close()
                except Exception:
                    pass
            if pages:
                self.page = pages[0]
        except Exception:
            pass

    def _new_tab(self):
        page = self._ctx.new_page()
        page.set_default_timeout(self.timeout_ms)
        return page

    def goto(self, url: str, wait_ms: int = 2500, page=None, wait_until="domcontentloaded"):
        page = page or self.page
        try:
            resp = page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
        except PWError as exc:
            if "Download is starting" in str(exc):
                return None, "download"
            raise
        page.wait_for_timeout(wait_ms)
        return resp, "ok"

    def text(self, page=None, retries: int = 2) -> str:
        page = page or self.page
        if (page.url or "").lower().split("?")[0].endswith(".pdf"):
            return ""
        for i in range(retries + 1):
            try:
                body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                if body or i == retries:
                    return body
            except Exception:
                pass
            try:
                page.wait_for_timeout(1200)
            except Exception:
                return ""
        return ""

    def title(self, page=None) -> str:
        page = page or self.page
        try:
            return page.title() or ""
        except Exception:
            return ""

    def is_bot_wall(self, page=None) -> bool:
        page = page or self.page
        t = self.title(page).lower()
        body = self.text(page)
        low = body.lower()
        url = (page.url or "").lower()
        if "just a moment" in t or "请稍候" in t or "attention required" in t:
            return True
        if "__cf_chl" in url or "/captcha" in url or "challenge" in t:
            return True
        if "are you a robot" in low or "performing security verification" in low:
            return True
        if "安全验证" in body or "请完成验证" in body:
            return True
        if len(body) < 400 and any(m in low for m in BOT_WALL_MARKERS):
            return True
        if len(body) < 400 and ("sciencedirect" in url or "karger" in url):
            # SD/Karger captcha pages render a very small shell
            return True
        return False

    def wait_past_wall(self, page=None, seconds: float = 25.0) -> bool:
        """Give a JS challenge a chance to clear on its own. Returns True if clear."""
        page = page or self.page
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self.is_bot_wall(page):
                return True
            page.wait_for_timeout(1500)
        return not self.is_bot_wall(page)

    # -- fetching ----------------------------------------------------------
    def request_bytes(self, url: str, referer: str | None = None,
                      accept: str = "application/pdf,*/*") -> bytes | None:
        headers = {"Accept": accept}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if referer:
            headers["Referer"] = referer
        try:
            resp = self._ctx.request.get(url, headers=headers, timeout=self.timeout_ms,
                                         max_redirects=10)
            body = resp.body()
            if body[:4] == b"%PDF":
                return body
        except Exception:
            pass
        return None

    def download_bytes(self, url: str, referer: str | None = None,
                       page=None) -> bytes | None:
        """Navigate to a URL that may trigger a browser download and return bytes."""
        tab = page or self._new_tab()
        own_tab = page is None
        tmpdir = Path(tempfile.mkdtemp(dir="/tmp"))
        try:
            tab.goto(referer or url, wait_until="domcontentloaded") if referer else None
            with tab.expect_download(timeout=self.timeout_ms) as dl:
                try:
                    tab.goto(url, wait_until="commit", timeout=self.timeout_ms)
                except PWError as exc:
                    if "Download is starting" not in str(exc):
                        raise
            download = dl.value
            path = tmpdir / "download.pdf"
            download.save_as(str(path))
            data = path.read_bytes()
            return data if data[:4] == b"%PDF" else None
        except Exception:
            return None
        finally:
            if own_tab:
                try:
                    tab.close()
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    def fetch_via_cdp(self, page, url: str, pattern: str = "*pdf.sciencedirectassets.com*",
                      wait_ms: int = 30_000) -> bytes | None:
        """Intercept the PDF response body via CDP (works for inline PDF viewers)."""
        import base64
        cdp = self._ctx.new_cdp_session(page)
        grabbed: dict = {}
        self.last_fetch_status = None

        def on_paused(event):
            rid = event.get("requestId")
            self.last_fetch_status = event.get("responseStatusCode")
            try:
                body = cdp.send("Fetch.getResponseBody", {"requestId": rid})
                if body.get("base64Encoded"):
                    grabbed["data"] = base64.b64decode(body["body"])
                else:
                    grabbed["data"] = body["body"].encode("latin-1")
            except Exception as exc:
                grabbed.setdefault("err", repr(exc)[:120])
            try:
                cdp.send("Fetch.continueRequest", {"requestId": rid})
            except Exception:
                pass

        try:
            cdp.send("Fetch.enable", {"patterns": [{"urlPattern": pattern,
                                                    "requestStage": "Response"}]})
            cdp.on("Fetch.requestPaused", on_paused)
            try:
                page.goto(url, wait_until="commit", timeout=60_000)
            except Exception:
                pass
            waited = 0
            while waited < wait_ms and not grabbed.get("data"):
                page.wait_for_timeout(500)
                waited += 500
        finally:
            try:
                cdp.send("Fetch.disable")
                cdp.detach()
            except Exception:
                pass
        data = grabbed.get("data")
        return data if data and data[:4] == b"%PDF" else None

    def find_pdf_links(self, page=None) -> list[str]:
        page = page or self.page
        try:
            links = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href], iframe[src], embed[src]'))
                    .map(e => e.href || e.src)
                    .filter(u => u && /pdf/i.test(u))"""
            )
        except Exception:
            links = []
        # de-duplicate, keep order
        seen, out = set(), []
        for link in links:
            if link not in seen:
                seen.add(link)
                out.append(link)
        return out[:10]
