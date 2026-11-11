import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
import uvicorn
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from trafilatura import extract as tf_extract

try:
    from trafilatura import extract_metadata as tf_metadata
except ImportError:
    from trafilatura.metadata import extract_metadata as tf_metadata

import scrape

API_TOKEN = os.environ.get("SCRAPER_TOKEN", "verander-dit-token")

mcp = FastMCP("mijn-scraper", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


def _get_html(url: str) -> str:
    if not scrape.robots_allows(url):
        raise ValueError(f"robots.txt van {urlparse(url).netloc} verbiedt dit")
    r = requests.get(url, headers={"User-Agent": scrape.USER_AGENT}, timeout=15)
    r.raise_for_status()
    time.sleep(scrape.DELAY_SECONDS)
    return r.text


def _meta_dict(html: str) -> dict:
    doc = tf_metadata(html)
    if doc is None:
        return {}
    meta = doc.as_dict() if hasattr(doc, "as_dict") else dict(vars(doc))
    keys = ("title", "author", "description", "sitename", "date", "pagetype", "lang", "hostname", "categories", "tags")
    return {k: meta.get(k) for k in keys if meta.get(k)}


@mcp.tool()
def fetch_page(url: str, format: str = "text", max_chars: int = 8000) -> str:
    """Lees een webpagina. format: text (default), markdown, html, links, metadata of summary."""
    html = _get_html(url)
    fmt = (format or "text").lower()
    if fmt in ("text", "markdown"):
        return (tf_extract(html) or "")[:max_chars]
    if fmt == "html":
        return html[:max_chars]
    if fmt == "links":
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.find_all("a", href=True):
            out.append({"url": urljoin(url, a["href"]).split("#")[0], "text": a.get_text(strip=True)[:120]})
        return str(out[:200])
    if fmt == "metadata":
        return str(_meta_dict(html))
    if fmt == "summary":
        text = tf_extract(html) or ""
        sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        return " ".join(sentences[:5])[:1200]
    raise ValueError(f"onbekend format '{format}' — kies: text, markdown, html, links, metadata, summary")


@mcp.tool()
def page_metadata(url: str) -> str:
    """Metadata van een pagina: titel, auteur, beschrijving, datum, paginatype, taal, sitenaam."""
    return str(_meta_dict(_get_html(url)))


@mcp.tool()
def map_site(url: str, limit: int = 100, search: str = "") -> str:
    """Lijst van URL's van een hele site (via sitemap.xml; anders links op de startpagina)."""
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    urls = set()
    try:
        r = requests.get(f"{base}/sitemap.xml", headers={"User-Agent": scrape.USER_AGENT}, timeout=15)
        if r.ok:
            root = ET.fromstring(r.text)
            for loc in root.iter("{*}loc"):
                if loc.text:
                    urls.add(loc.text.strip())
    except Exception:
        pass
    if not urls:
        html = _get_html(url)
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0]
            if link.startswith("http") and urlparse(link).netloc == urlparse(url).netloc:
                urls.add(link)
    result = sorted(urls)
    if search:
        result = [u for u in result if search.lower() in u.lower()]
    return str(result[:limit])


@mcp.tool()
def crawl_site(url: str, max_pages: int = 10, max_chars: int = 4000) -> dict:
    """Haal een pagina op en volg links binnen hetzelfde domein (max max_pages pagina's)."""
    results = {}
    to_visit = [url]
    seen = set()
    while to_visit and len(results) < max_pages:
        current = to_visit.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            html = _get_html(current)
            results[current] = (tf_extract(html) or "")[:max_chars]
            if urlparse(current).netloc == urlparse(url).netloc:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    link = urljoin(current, a["href"]).split("#")[0]
                    if link.startswith("http") and urlparse(link).netloc == urlparse(url).netloc and link not in seen:
                        to_visit.append(link)
        except Exception as e:
            results[current] = f"FOUT: {e}"
    return results


class TokenCheck(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if request.headers.get("x-scraper-token") != API_TOKEN and auth != f"Bearer {API_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.add_middleware(TokenCheck)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
