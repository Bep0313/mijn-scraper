import json
import os
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urljoin, urlparse

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
CACHE_TTL = 3600          # responses 1 uur cachen
MIN_DOMAIN_DELAY = 2.0    # min. 2 sec tussen requests naar zelfde domein

mcp = FastMCP("mijn-scraper", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

_response_cache = {}
_robots_cache = {}
_last_request = {}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _throttle(domain: str) -> None:
    last = _last_request.get(domain, 0)
    wait = MIN_DOMAIN_DELAY - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_request[domain] = time.time()


def _robots_allows(url: str) -> bool:
    domain = _domain(url)
    now = time.time()
    cached = _robots_cache.get(domain)
    if cached and now - cached[0] < CACHE_TTL:
        rp = cached[1]
    else:
        p = urlparse(url)
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
        try:
            rp.read()
        except Exception:
            pass
        _robots_cache[domain] = (now, rp)
    return rp.can_fetch(scrape.USER_AGENT, url)


def _fetch_raw(url: str):
    """Haalt HTML of PDF op met robots-check, per-domein throttling en cache. Geeft (type, data) terug."""
    if scrape.ALLOWED_DOMAINS and _domain(url) not in scrape.ALLOWED_DOMAINS:
        raise ValueError(f"Domein niet toegestaan: {_domain(url)}")
    if not _robots_allows(url):
        raise ValueError(f"robots.txt van {_domain(url)} verbiedt dit")
    now = time.time()
    cached = _response_cache.get(url)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1], cached[2]
    _throttle(_domain(url))
    r = requests.get(url, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        result = ("pdf", r.content)
    else:
        result = ("html", r.text)
    _response_cache[url] = (now, result[0], result[1])
    return result


def _extract_text(url: str, max_chars=None) -> str:
    typ, data = _fetch_raw(url)
    if typ == "pdf":
        text = tf_extract(filecontent=data, url=url) or ""
    else:
        text = tf_extract(data) or ""
    return text if max_chars is None else text[:max_chars]


def _meta_dict(html: str) -> dict:
    doc = tf_metadata(html)
    if doc is None:
        return {}
    meta = doc.as_dict() if hasattr(doc, "as_dict") else dict(vars(doc))
    keys = ("title", "author", "description", "sitename", "date", "pagetype", "lang", "hostname", "categories", "tags")
    return {k: _clean(meta.get(k)) for k in keys if meta.get(k)}


@mcp.tool()
def fetch_page(url: str, format: str = "text", max_chars: int = 8000, link_filter: str = "") -> dict:
    """Lees een webpagina (HTML, PDF of JSON-endpoint).
    format: text (default) | markdown | html | json | links | metadata | summary
    link_filter: optionele regex waar link-URLs aan moeten voldoen (alleen bij format=links)
    """
    typ, data = _fetch_raw(url)
    fmt = (format or "text").lower()
    if fmt in ("text", "markdown"):
        return {"format": fmt, "url": url, "text": _extract_text(url, max_chars)}
    if fmt == "html":
        if typ == "pdf":
            raise ValueError("PDF heeft geen html-format; gebruik text/markdown/summary")
        return {"format": "html", "url": url, "html": data[:max_chars]}
    if fmt == "json":
        if typ == "pdf":
            raise ValueError("PDF is geen JSON")
        return {"format": "json", "url": url, "json": json.loads(data)}
    if fmt == "links":
        if typ == "pdf":
            raise ValueError("PDF heeft geen links; gebruik text/markdown/summary")
        soup = BeautifulSoup(data, "lxml")
        out = []
        for a in soup.find_all("a", href=True):
            link = _clean(urljoin(url, a["href"]).split("#")[0])
            if link_filter and not re.search(link_filter, link):
                continue
            out.append({"url": link, "text": _clean(a.get_text(strip=True))[:120]})
            if len(out) >= 500:
                break
        return {"format": "links", "url": url, "count": len(out), "links": out}
    if fmt == "metadata":
        if typ == "pdf":
            return {"format": "metadata", "url": url, "metadata": {"pagetype": "pdf"}}
        return {"format": "metadata", "url": url, "metadata": _meta_dict(data)}
    if fmt == "summary":
        text = _extract_text(url)
        sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        return {"format": "summary", "url": url, "summary": " ".join(sentences[:5])[:1200]}
    raise ValueError(f"onbekend format '{format}' — kies: text, markdown, html, json, links, metadata, summary")


@mcp.tool()
def page_metadata(url: str) -> dict:
    """Metadata van een pagina: titel, auteur, beschrijving, datum, paginatype, taal, sitenaam, categories, tags."""
    typ, data = _fetch_raw(url)
    if typ == "pdf":
        return {"url": url, "type": "pdf", "metadata": {"pagetype": "pdf"}}
    return {"url": url, "type": "html", "metadata": _meta_dict(data)}


@mcp.tool()
def map_site(url: str, limit: int = 200, search: str = "", link_filter: str = "") -> dict:
    """Lijst van URL's van een hele site: via sitemap.xml (recursief), anders via links op de startpagina."""
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    urls = set()

    def parse_sitemap(sm_url: str, depth: int = 0):
        if depth > 3:
            return
        try:
            if not _robots_allows(sm_url):
                return
            _throttle(_domain(sm_url))
            r = requests.get(sm_url, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
            if not r.ok:
                return
            root = ET.fromstring(r.text)
            for loc in root.iter("{*}loc"):
                if not loc.text:
                    continue
                u = _clean(loc.text)
                if u.lower().endswith(".xml"):
                    parse_sitemap(u, depth + 1)
                else:
                    urls.add(u)
        except Exception:
            pass

    parse_sitemap(f"{base}/sitemap.xml")
    if not urls:
        typ, data = _fetch_raw(url)
        if typ == "html":
            soup = BeautifulSoup(data, "lxml")
            for a in soup.find_all("a", href=True):
                link = _clean(urljoin(url, a["href"]).split("#")[0])
                if link.startswith("http") and _domain(link) == _domain(url):
                    urls.add(link)
    result = sorted(urls)
    if search:
        result = [u for u in result if search.lower() in u.lower()]
    if link_filter:
        result = [u for u in result if re.search(link_filter, u)]
    return {"site": base, "total": len(result), "urls": result[:limit]}


@mcp.tool()
def crawl_site(url: str, max_pages: int = 10, max_chars: int = 4000, link_filter: str = "") -> dict:
    """Haal de startpagina op en volg links binnen hetzelfde domein (max max_pages)."""
    results = {}
    to_visit = [url]
    seen = set()
    while to_visit and len(results) < max_pages:
        current = to_visit.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            typ, data = _fetch_raw(current)
            results[current] = {"type": typ, "text": _extract_text(current, max_chars)}
            if typ == "html" and _domain(current) == _domain(url):
                soup = BeautifulSoup(data, "lxml")
                for a in soup.find_all("a", href=True):
                    link = _clean(urljoin(current, a["href"]).split("#")[0])
                    if link.startswith("http") and _domain(link) == _domain(url) and link not in seen:
                        if link_filter and not re.search(link_filter, link):
                            continue
                        to_visit.append(link)
        except Exception as e:
            results[current] = {"error": str(e)}
    return {"start": url, "pages": len(results), "results": results}


PRICE_RE = re.compile(r"(?:€|EUR)\s?(\d{1,6}(?:[.,]\d{1,2})?)|(\d{1,6}[.,]\d{2})\s?(?:€|EUR)", re.IGNORECASE)


def _parse_price(text):
    text = str(text).strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _walk_jsonld(obj):
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and "Product" in x for x in types):
            yield obj
        for v in obj.values():
            yield from _walk_jsonld(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_jsonld(v)


def _do_extract_products(url: str) -> dict:
    fetched_at = _now()
    typ, data = _fetch_raw(url)
    if typ == "pdf":
        return {"url": url, "shop": _domain(url), "fetched_at": fetched_at, "count": 0, "products": [], "note": "PDF: geen product-extractie"}
    html = data
    products = []
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data_json = json.loads(script.string or "")
        except Exception:
            continue
        for p in _walk_jsonld(data_json):
            offers = p.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                offers = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                price = o.get("price")
                if price is None and isinstance(o.get("priceSpecification"), dict):
                    price = o["priceSpecification"].get("price")
                brand = p.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                availability = str(o.get("availability", ""))
                products.append({
                    "name": _clean(p.get("name") or ""),
                    "price": _parse_price(price) if price is not None else None,
                    "currency": o.get("priceCurrency", "EUR"),
                    "available": availability.endswith("InStock"),
                    "availability": availability.split("/")[-1],
                    "brand": _clean(brand or ""),
                    "vendor": _clean(p.get("vendor") or ""),
                    "product_type": _clean(p.get("product_type") or ""),
                    "tags": [_clean(t) for t in (p.get("tags") or []) if isinstance(t, str)],
                    "url": url,
                    "shop": _domain(url),
                    "source": "json-ld",
                    "fetched_at": fetched_at,
                })
    if not products:
        text = tf_extract(html) or ""
        m = PRICE_RE.search(text)
        if m:
            og = soup.find("meta", property="og:title")
            title = og["content"] if og and og.get("content") else (soup.title.get_text(strip=True) if soup.title else "")
            products.append({
                "name": _clean(title),
                "price": _parse_price(m.group(1) or m.group(2)),
                "currency": "EUR",
                "available": None,
                "availability": "",
                "brand": "",
                "vendor": "",
                "product_type": "",
                "tags": [],
                "url": url,
                "shop": _domain(url),
                "source": "regex-fallback",
                "fetched_at": fetched_at,
            })
    return {"url": url, "shop": _domain(url), "fetched_at": fetched_at, "count": len(products), "products": products}


@mcp.tool()
def extract_products(url: str) -> dict:
    """Haal product-/prijsinformatie uit een productpagina (JSON-LD Product/Offer eerst, regex als fallback). Alle velden gestript, met fetched_at."""
    return _do_extract_products(url)


@mcp.tool()
def compare_prices(urls: str) -> dict:
    """Vergelijk prijzen over meerdere productpagina's.
    urls: komma- of enter-gescheiden lijst van URL's. Geeft alle offers terug, gesorteerd op prijs.
    """
    all_offers = []
    errors = {}
    for u in [x.strip() for x in re.split(r"[,\n]", urls) if x.strip()]:
        try:
            all_offers.extend(_do_extract_products(u)["products"])
        except Exception as e:
            errors[u] = str(e)
    with_price = sorted([o for o in all_offers if o.get("price") is not None], key=lambda o: (o["price"], o.get("shop", "")))
    return {
        "fetched_at": _now(),
        "offers_with_price": len(with_price),
        "sorted_by_price": with_price,
        "without_price": [o for o in all_offers if o.get("price") is None],
        "errors": errors,
        "note": "prijzen excl. Produitkosten; controleer available per winkel",
    }


@mcp.tool()
def search_web(query: str, num_results: int = 10, site: str = "") -> dict:
    """Zoek op het web via DuckDuckGo (html-versie). site: bv '.be', '.nl' of 'bol.com' om te beperken tot een domein."""
    q = query + (f" site:{site}" if site else "")
    if not _robots_allows("https://html.duckduckgo.com/html/"):
        raise ValueError("robots.txt van DuckDuckGo verbiedt dit; gebruik firecrawl_search als fallback")
    _throttle("html.duckduckgo.com")
    r = requests.get("https://html.duckduckgo.com/html/", params={"q": q}, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for res in soup.select(".result"):
        a = res.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            href = unquote(m.group(1))
        snippet_el = res.select_one(".result__snippet")
        out.append({
            "title": _clean(a.get_text(strip=True)),
            "url": _clean(href),
            "snippet": _clean(snippet_el.get_text(strip=True)) if snippet_el else "",
        })
        if len(out) >= num_results:
            break
    return {"query": q, "count": len(out), "results": out}


@mcp.tool()
def shopify_products(domain: str, collection: str = "all", max_products: int = 1000, available_only: bool = False) -> dict:
    """Lees de productcatalogus van een Shopify-shop via de publieke JSON-API (/products.json), met paginering.
    domain: bv. 'parfumado.com'. collection: 'all' (hele shop) of een collectie-slug bv. 'parfums'.
    Geeft schone records: title, vendor, product_type, tags, price, sku, available (bool), url, image, body_html, fetched_at.
    """
    domain = _clean(domain).replace("https://", "").replace("http://", "").strip("/")
    base = f"https://{domain}"
    fetched_at = _now()
    products = []
    page = 1
    while len(products) < max_products:
        if collection and collection != "all":
            api = f"{base}/collections/{collection}/products.json"
        else:
            api = f"{base}/products.json"
        if not _robots_allows(api):
            raise ValueError(f"robots.txt van {domain} verbiedt dit")
        _throttle(domain)
        r = requests.get(api, params={"limit": 250, "page": page}, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
        if r.status_code == 404:
            raise ValueError(f"geen products.json op {base} — geen Shopify-shop of endpoint geblokkeerd")
        r.raise_for_status()
        batch = r.json().get("products", [])
        if not batch:
            break
        for p in batch:
            variants = p.get("variants") or []
            v0 = variants[0] if variants else {}
            products.append({
                "title": _clean(p.get("title", "")),
                "vendor": _clean(p.get("vendor", "")),
                "product_type": _clean(p.get("product_type", "")),
                "tags": [_clean(t) for t in (p.get("tags") or []) if isinstance(t, str)],
                "price": _parse_price(v0.get("price")) if v0.get("price") is not None else None,
                "sku": _clean(v0.get("sku", "")),
                "available": bool(v0.get("available", False)),
                "url": f"{base}/products/{_clean(p.get('handle', ''))}",
                "image": (p.get("images") or [{}])[0].get("src", ""),
                "body_html": p.get("body_html", ""),
                "fetched_at": fetched_at,
            })
        if len(batch) < 250:
            break
        page += 1
    if available_only:
        products = [p for p in products if p["available"]]
    return {"shop": domain, "collection": collection, "fetched_at": fetched_at, "count": len(products), "products": products[:max_products]}


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
