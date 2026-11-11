import hmac
import io
import ipaddress
import json
import os
import re
import socket
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import OrderedDict
from urllib.parse import unquote, urljoin, urlparse

import requests
import uvicorn
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader
from starlette.responses import JSONResponse
from starlette.routing import Route
from trafilatura import extract as tf_extract

try:
    from trafilatura import extract_metadata as tf_metadata
except ImportError:
    from trafilatura.metadata import extract_metadata as tf_metadata

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

import scrape

API_TOKEN = os.environ.get("SCRAPER_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError("SCRAPER_TOKEN is niet ingesteld (zet deze als Environment variable in Render)")

CACHE_TTL = 3600
MAX_CACHE_ITEMS = 100
MIN_DOMAIN_DELAY = 1.5

mcp = FastMCP(
    "mijn-scraper",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
    stateless_http=True,
    json_response=True,
    streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
)

_response_cache = OrderedDict()
_robots_cache = OrderedDict()
_last_request = {}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _check_safe_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("Alleen http/https URL's toegestaan")
    host = p.hostname
    if not host:
        raise ValueError("Ongeldige URL")
    if scrape.ALLOWED_DOMAINS and host.lower() not in scrape.ALLOWED_DOMAINS:
        raise ValueError(f"Domein niet toegestaan: {host}")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except socket.gaierror:
        raise ValueError(f"Domeinnaam onvindbaar: {host}")
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError(f"Intern netwerkadres geweigerd: {host}")


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
        return cached[1]
    p = urlparse(url)
    allowed = True
    try:
        r = requests.get(
            f"{p.scheme}://{p.netloc}/robots.txt",
            headers={"User-Agent": scrape.USER_AGENT},
            timeout=5,
        )
        if r.status_code == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            allowed = rp.can_fetch(scrape.USER_AGENT, url)
    except Exception:
        allowed = True
    if len(_robots_cache) >= MAX_CACHE_ITEMS:
        _robots_cache.popitem(last=False)
    _robots_cache[domain] = (now, allowed)
    return allowed


def _fetch_raw(url: str):
    _check_safe_url(url)
    if not _robots_allows(url):
        raise ValueError(f"robots.txt van {_domain(url)} verbiedt dit")
    now = time.time()
    cached = _response_cache.get(url)
    if cached and now - cached[0] < CACHE_TTL:
        _response_cache.move_to_end(url)
        return cached[1], cached[2]
    _throttle(_domain(url))
    r = requests.get(url, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        result = ("pdf", r.content)
    else:
        result = ("html", r.text)
    if len(result[1]) < 2_000_000:
        if len(_response_cache) >= MAX_CACHE_ITEMS:
            _response_cache.popitem(last=False)
        _response_cache[url] = (now, result[0], result[1])
    return result


def _extract_text(url: str, max_chars=None, as_markdown: bool = False) -> str:
    typ, data = _fetch_raw(url)
    if typ == "pdf":
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    elif as_markdown:
        text = tf_extract(data, output_format="markdown", include_tables=True, include_links=True) or ""
    else:
        text = tf_extract(data, output_format="txt") or ""
    return text if max_chars is None else text[:max_chars]


def _meta_dict(html: str) -> dict:
    doc = tf_metadata(html)
    if doc is None:
        return {}
    meta = doc.as_dict() if hasattr(doc, "as_dict") else dict(vars(doc))
    keys = ("title", "author", "description", "sitename", "date", "pagetype", "lang", "hostname", "categories", "tags")
    return {k: _clean(meta.get(k)) for k in keys if meta.get(k)}


@mcp.tool()
def fetch_page(url: str, format: str = "markdown", max_chars: int = 8000, link_filter: str = "") -> dict:
    """Lees een webpagina (HTML, PDF of JSON-endpoint).
    format: markdown (default) | text | html | json | links | metadata | summary
    link_filter: optionele regex waar link-URLs aan moeten voldoen (alleen bij format=links)
    """
    typ, data = _fetch_raw(url)
    fmt = (format or "markdown").lower()
    if fmt in ("text", "markdown"):
        return {"format": fmt, "url": url, "text": _extract_text(url, max_chars, as_markdown=(fmt == "markdown"))}
    if fmt == "html":
        if typ == "pdf":
            raise ValueError("PDF heeft geen html-format; gebruik markdown/text/summary")
        return {"format": "html", "url": url, "html": data[:max_chars]}
    if fmt == "json":
        if typ == "pdf":
            raise ValueError("PDF is geen JSON")
        return {"format": "json", "url": url, "json": json.loads(data)}
    if fmt == "links":
        if typ != "html":
            raise ValueError("Geen directe HTML; gebruik markdown/text")
        soup = BeautifulSoup(data, "lxml")
        out = []
        for a in soup.find_all("a", href=True):
            link = _clean(urljoin(url, a["href"]).split("#")[0])
            if link_filter and not re.search(link_filter, link):
                continue
            out.append({"url": link, "text": _clean(a.get_text(strip=True))[:120]})
            if len(out) >= 300:
                break
        return {"format": "links", "url": url, "count": len(out), "links": out}
    if fmt == "metadata":
        if typ == "pdf":
            return {"format": "metadata", "url": url, "metadata": {"pagetype": "pdf"}}
        return {"format": "metadata", "url": url, "metadata": _meta_dict(data)}
    if fmt == "summary":
        text = _extract_text(url)
        sentences = re.split(r"(?<=[.!?])\\s+", text.replace("\n", " "))
        return {"format": "summary", "url": url, "summary": " ".join(sentences[:5])[:1200]}
    raise ValueError(f"Onbekend format '{format}' — kies: markdown, text, html, json, links, metadata, summary")


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
    _check_safe_url(url)
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    urls = set()

    def parse_sitemap(sm_url: str, depth: int = 0):
        if depth > 2 or len(urls) >= 5000:
            return
        try:
            if not _robots_allows(sm_url):
                return
            _throttle(_domain(sm_url))
            r = requests.get(sm_url, headers={"User-Agent": scrape.USER_AGENT}, timeout=15)
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
def crawl_site(url: str, max_pages: int = 5, max_chars: int = 4000, link_filter: str = "") -> dict:
    """Haal de startpagina op en volg links binnen hetzelfde domein (max 15 pagina's)."""
    max_pages = min(max(int(max_pages), 1), 15)
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
            results[current] = {"type": typ, "text": _extract_text(current, max_chars, as_markdown=True)}
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


PRICE_RE = re.compile(r"(?:€|EUR)\\s?(\\d{1,6}(?:[.,]\\d{1,2})?)|(\\d{1,6}[.,]\\d{2})\\s?(?:€|EUR)", re.IGNORECASE)


def _parse_price(text):
    if text is None:
        return None
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
                price = o.get("price") or o.get("lowPrice")
                if price is None and isinstance(o.get("priceSpecification"), dict):
                    price = o["priceSpecification"].get("price") or o["priceSpecification"].get("minPrice")
                brand = p.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                availability = str(o.get("availability", ""))
                products.append({
                    "name": _clean(p.get("name") or ""),
                    "price": _parse_price(price),
                    "currency": o.get("priceCurrency", "EUR"),
                    "available": "instock" in availability.lower(),
                    "availability": availability.split("/")[-1],
                    "brand": _clean(brand or ""),
                    "sku": _clean(o.get("sku") or p.get("sku") or ""),
                    "category": _clean(p.get("category") or ""),
                    "url": _clean(o.get("url") or url),
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
                "sku": "",
                "category": "",
                "url": url,
                "shop": _domain(url),
                "source": "regex-fallback",
                "fetched_at": fetched_at,
            })
    return {"url": url, "shop": _domain(url), "fetched_at": fetched_at, "count": len(products), "products": products}


@mcp.tool()
def extract_products(url: str) -> dict:
    """Haal product-/prijsinformatie uit een productpagina (JSON-LD Product/Offer incl. lowPrice, regex als fallback)."""
    return _do_extract_products(url)


@mcp.tool()
def compare_prices(urls: str) -> dict:
    """Vergelijk prijzen over meerdere productpagina's.
    urls: komma- of enter-gescheiden lijst van URL's. Geeft alle offers terug, gesorteerd op prijs.
    """
    all_offers = []
    errors = {}
    for u in [x.strip() for x in re.split(r"[,\\n]", urls) if x.strip()]:
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
        "note": "Prijzen zijn excl. verzendkosten; controleer de voorraadstatus per winkel.",
    }


@mcp.tool()
def search_web(query: str, num_results: int = 10, site: str = "") -> dict:
    """Zoek op het web. site: bv '.be', '.nl' of 'bol.com' om te beperken tot een domein."""
    q = query + (f" site:{site}" if site else "")
    num_results = min(max(int(num_results), 1), 25)
    if DDGS is not None:
        try:
            results = DDGS().text(q, max_results=num_results)
            out = [
                {"title": _clean(r.get("title", "")), "url": _clean(r.get("href", "")), "snippet": _clean(r.get("body", ""))}
                for r in results
            ]
            if out:
                return {"query": q, "count": len(out), "results": out, "source": "ddgs"}
        except Exception:
            pass
    _throttle("html.duckduckgo.com")
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
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
        snip = res.select_one(".result__snippet")
        out.append({
            "title": _clean(a.get_text(strip=True)),
            "url": _clean(href),
            "snippet": _clean(snip.get_text(strip=True)) if snip else "",
        })
        if len(out) >= num_results:
            break
    return {"query": q, "count": len(out), "results": out, "source": "html-fallback"}


@mcp.tool()
def shopify_products(domain: str, collection: str = "all", search: str = "", max_products: int = 50, available_only: bool = False) -> dict:
    """Lees de productcatalogus van een Shopify-shop via de publieke /products.json API (paginering).
    domain: bv. 'parfumado.com'. collection: 'all' of een collectie-slug.
    search: optionele filter op titel, vendor of tags. max_products: default 50 (beschermt de AI-context).
    """
    domain = _clean(domain).replace("https://", "").replace("http://", "").strip("/")
    base = f"https://{domain}"
    _check_safe_url(base)
    fetched_at = _now()
    max_products = min(max(int(max_products), 1), 1000)
    products = []
    search_low = search.lower().strip()
    page = 1
    while len(products) < max_products and page <= 10:
        if collection and collection != "all":
            api = f"{base}/collections/{collection}/products.json"
        else:
            api = f"{base}/products.json"
        if not _robots_allows(api):
            raise ValueError(f"robots.txt van {domain} verbiedt dit")
        _throttle(domain)
        r = requests.get(api, params={"limit": 250, "page": page}, headers={"User-Agent": scrape.USER_AGENT}, timeout=20)
        if r.status_code == 404:
            raise ValueError(f"Geen products.json op {base} — geen Shopify-shop of endpoint geblokkeerd")
        r.raise_for_status()
        batch = r.json().get("products", [])
        if not batch:
            break
        for p in batch:
            variants = p.get("variants") or []
            v0 = variants[0] if variants else {}
            available = any(bool(v.get("available")) for v in variants) if variants else False
            if available_only and not available:
                continue
            title = _clean(p.get("title", ""))
            vendor = _clean(p.get("vendor", ""))
            raw_tags = p.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = raw_tags.split(",")
            tags = [_clean(t) for t in raw_tags if isinstance(t, str) and t.strip()]
            body = p.get("body_html") or ""
            desc = BeautifulSoup(body, "lxml").get_text(" ", strip=True)[:350] if body else ""
            if search_low and search_low not in f"{title} {vendor} {' '.join(tags)} {desc}".lower():
                continue
            products.append({
                "title": title,
                "vendor": vendor,
                "product_type": _clean(p.get("product_type", "")),
                "tags": tags,
                "price": _parse_price(v0.get("price")),
                "sku": _clean(v0.get("sku", "")),
                "available": available,
                "url": f"{base}/products/{_clean(p.get('handle', ''))}",
                "image": (p.get("images") or [{}])[0].get("src", ""),
                "description": desc,
                "fetched_at": fetched_at,
            })
            if len(products) >= max_products:
                break
        if len(batch) < 250:
            break
        page += 1
    return {"shop": domain, "collection": collection, "search": search, "fetched_at": fetched_at, "count": len(products), "products": products}


async def health(request):
    return JSONResponse({"status": "ok", "service": "mijn-scraper", "time": _now()})


class TokenAuthASGI:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in ("/", "/health"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        token = headers.get("x-scraper-token", "")
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not (hmac.compare_digest(token, API_TOKEN) or hmac.compare_digest(bearer, API_TOKEN)):
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", health, methods=["GET"]))
    uvicorn.run(TokenAuthASGI(app), host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
    
