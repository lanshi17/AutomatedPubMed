"""PDF byte fetching.

Three layers, cheapest first:
  1. curl from the campus IP with redirects and *resumable* retries
     (some publishers serve PDFs from object storage over slow routes; a
     120 s cap can truncate a 7 MB file into an unreadable stub)
  2. Chrome request context (inherits cookies from pages we rendered)
  3. Chrome download event (content-disposition attachments, JS-driven PDFs)

Everything goes out without the machine's proxy: publisher entitlement is tied
to the campus egress address, and a proxy/VPN would lose it.
"""
from __future__ import annotations

import os
import re
import time
import subprocess
import tempfile
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    return env


def _content_length(header_file: Path) -> int | None:
    try:
        text = header_file.read_text(errors="ignore")
    except OSError:
        return None
    hits = re.findall(r"content-length:\s*(\d+)", text, re.I)
    return int(hits[-1]) if hits else None


def curl_pdf(url: str, referer: str | None = None, timeout: int = 120,
             max_attempts: int = 6, max_total_s: int = 300,
             proxy: str | None = None) -> bytes | None:
    fd, path = tempfile.mkstemp(suffix=".pdf", dir="/tmp")
    os.close(fd)
    hdr = Path(path + ".hdr")
    deadline = time.time() + max_total_s
    try:
        for _ in range(max_attempts):
            if time.time() > deadline:
                return None
            cmd = [
                "curl", "-sL", "--compressed",
                "-A", UA, "-H", "Accept: application/pdf,*/*",
                "-m", str(timeout), "-C", "-",
                "-D", str(hdr), "-o", path, url,
            ]
            if proxy:
                cmd[1:1] = ["--proxy", proxy]
            else:
                cmd[1:1] = ["--noproxy", "*"]
            if referer:
                cmd += ["-e", referer]
            try:
                subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=timeout + 20)
            except Exception:
                pass
            size = Path(path).stat().st_size if Path(path).exists() else 0
            total = _content_length(hdr)
            data_head = b""
            if size:
                with open(path, "rb") as fh:
                    data_head = fh.read(4)
            if data_head != b"%PDF":
                return None
            if total is None or size >= total:
                return Path(path).read_bytes()
            if size == 0:
                return None
        # could not complete; refuse a truncated file
        return None
    finally:
        Path(path).unlink(missing_ok=True)
        hdr.unlink(missing_ok=True)


def fetch_pdf(url: str, referer: str | None, browser, timeout_ms: int = 120_000,
              browser_fallback: bool = True, proxy: str | None = None,
              curl_first: bool = True) -> bytes | None:
    """Fetch a PDF, cheapest path first.

    `curl_first=False` is for links found on an article page: those sites sit
    behind a JS challenge, so bare curl has no cookies and would just stall for
    its full timeout. Going to the browser session first is both faster and the
    only path that actually carries entitlement cookies.
    """
    def _curl():
        return curl_pdf(url, referer=referer, timeout=min(240, max(30, timeout_ms // 1000)),
                        proxy=proxy)

    if curl_first:
        data = _curl()
        if data:
            return data
    if proxy or not browser_fallback or browser is None:
        return _curl() if not curl_first else None
    try:
        resp = browser.context.request.get(
            url,
            headers={"Accept": "application/pdf,*/*", "Referer": referer or url},
            timeout=min(timeout_ms, 60_000),
            max_redirects=10,
        )
        body = resp.body()
        if body[:4] == b"%PDF":
            return body
    except Exception:
        pass
    if not curl_first:
        data = _curl()
        if data:
            return data
    try:
        return browser.download_bytes(url, referer=referer)
    except Exception:
        return None
