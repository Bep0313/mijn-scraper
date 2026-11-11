import sys
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
import trafilatura

ALLOWED_DOMAINS = []
USER_AGENT = "mijn-scraper/1.0 (eigen gebruik)"
DELAY_SECONDS = 1.0
MAX_CHARS = 8000


def robots_allows(url: str) -> bool:
    p = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
    try:
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def fetch_page(url: str, max_chars: int = MAX_CHARS) -> str:
    domain = urlparse(url).netloc
    if ALLOWED_DOMAINS and domain not in ALLOWED_DOMAINS:
        raise ValueError(f"Domein niet toegestaan: {domain}")
    if not robots_allows(url):
        raise ValueError(f"robots.txt van {domain} verbiedt dit")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    text = trafilatura.extract(r.text) or ""
    time.sleep(DELAY_SECONDS)
    return text[:max_chars]


if __name__ == "__main__":
    print(fetch_page(sys.argv[1]))
