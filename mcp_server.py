import os
import time
from urllib.parse import urljoin, urlparse

import requests
import uvicorn
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import scrape

API_TOKEN = os.environ.get("SCRAPER_TOKEN", "verander-dit-token")

mcp = FastMCP("mijn-scraper", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


@mcp.tool()
def fetch_page(url: str, max_chars: int = 8000) -> str:
    """Lees een webpagina en geef de schone platte tekst terug."""
    return scrape.fetch_page(url, max_chars)


@mcp.tool()
def crawl_site(url: str, max_pages: int = 10, max_chars: int = 4000) -> dict:
    """Haal een pagina op en volg links binnen hetzelfde domein (max max_pages)."""
    results = {}
    to_visit = [url]
    seen = set()
    while to_visit and len(results) < max_pages:
        current = to_visit.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            r = requests.get(current, headers={"User-Agent": scrape.USER_AGENT}, timeout=15)
            r.raise_for_status()
            results[current] = scrape.fetch_page(current, max_chars)
            if urlparse(current).netloc == urlparse(url).netloc:
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.find_all("a", href=True):
                    link = urljoin(current, a["href"]).split("#")[0]
                    if link.startswith("http") and urlparse(link).netloc == urlparse(url).netloc and link not in seen:
                        to_visit.append(link)
        except Exception as e:
            results[current] = f"FOUT: {e}"
        time.sleep(scrape.DELAY_SECONDS)
    return results


class TokenCheck(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get("x-scraper-token") != API_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.add_middleware(TokenCheck)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
