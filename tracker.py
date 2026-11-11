import os
import time

import requests
from starlette.responses import JSONResponse

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
API_TOKEN = os.environ.get("SCRAPER_TOKEN", "")

_extract = None
_now = None


def _sb_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _sb_headers(prefer: str = "") -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_upsert(table: str, row: dict, on_conflict: str):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params={"on_conflict": on_conflict},
        headers=_sb_headers("resolution=merge-duplicates,return=representation"),
        json=row,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def sb_insert(table: str, row: dict):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=_sb_headers("return=representation"),
        json=row,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def sb_select(table: str, params: dict):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_sb_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _price_of(url: str):
    res = _extract(url)
    prods = [p for p in (res.get("products") or []) if p.get("price") is not None]
    if not prods:
        raise ValueError("geen prijs gevonden op de pagina")
    best = min(prods, key=lambda p: p["price"])
    return best["price"], best.get("name") or ""


def register(mcp, extract_fn, now_fn) -> None:
    global _extract, _now
    _extract, _now = extract_fn, now_fn

    @mcp.tool()
    def track_product(url: str, name: str = "") -> dict:
        """Zet een productpagina op de prijsvolglijst: scrapet nu 1x de prijs en slaat die op (Supabase)."""
        if not _sb_ready():
            return {"error": "Supabase niet geconfigureerd: zet SUPABASE_URL en SUPABASE_SERVICE_KEY in Render"}
        price, pname = _price_of(url)
        name = name or pname
        ts = _now()
        sb_upsert("tracked_products", {"url": url, "name": name, "current_price": price, "last_checked": ts}, "url")
        sb_insert("price_points", {"product_url": url, "price": price, "checked_at": ts})
        return {"tracked": True, "url": url, "name": name, "price": price, "checked_at": ts}

    @mcp.tool()
    def price_history(url: str) -> dict:
        """Geef de opgeslagen prijs-geschiedenis van een product (punten, min, avg, huidige prijs)."""
        if not _sb_ready():
            return {"error": "Supabase niet geconfigureerd: zet SUPABASE_URL en SUPABASE_SERVICE_KEY in Render"}
        pts = sb_select("price_points", {"product_url": f"eq.{url}", "select": "price,checked_at", "order": "checked_at.asc"})
        prices = [p["price"] for p in pts if p.get("price") is not None]
        tracked = sb_select("tracked_products", {"url": f"eq.{url}", "select": "name,current_price,last_checked"})
        return {
            "url": url,
            "name": tracked[0]["name"] if tracked else None,
            "current_price": tracked[0]["current_price"] if tracked else None,
            "count": len(prices),
            "min": min(prices) if prices else None,
            "avg": round(sum(prices) / len(prices), 2) if prices else None,
            "points": pts,
        }

    @mcp.tool()
    def deal_check(url: str) -> dict:
        """Check of de huidige prijs een deal is: vs. historisch minimum en gemiddelde."""
        if not _sb_ready():
            return {"error": "Supabase niet geconfigureerd: zet SUPABASE_URL en SUPABASE_SERVICE_KEY in Render"}
        hist = sb_select("price_points", {"product_url": f"eq.{url}", "select": "price"})
        prices = [p["price"] for p in hist if p.get("price") is not None]
        tracked = sb_select("tracked_products", {"url": f"eq.{url}", "select": "name,current_price,last_checked"})
        if not tracked:
            return {"error": "product staat niet op de volglijst — gebruik eerst track_product"}
        current = tracked[0].get("current_price")
        if current is None:
            return {"error": "nog geen prijs gemeten"}
        mn = min(prices) if prices else current
        avg = round(sum(prices) / len(prices), 2) if prices else current
        above_min_pct = round((current - mn) / mn * 100, 1) if mn else 0
        below_avg_pct = round((avg - current) / avg * 100, 1) if avg else 0
        if current <= mn * 1.05:
            verdict = "DEAL — op/bij het historisch minimum"
        elif below_avg_pct > 0:
            verdict = f"onder het gemiddelde ({below_avg_pct}% lager dan gemiddeld)"
        else:
            verdict = f"boven het gemiddelde ({-below_avg_pct}% duurder dan gemiddeld)"
        return {
            "url": url,
            "name": tracked[0].get("name"),
            "current_price": current,
            "min_ever": mn,
            "avg": avg,
            "above_min_pct": above_min_pct,
            "below_avg_pct": below_avg_pct,
            "measurements": len(prices),
            "verdict": verdict,
        }


async def scrape_all_endpoint(request):
    auth = request.headers.get("authorization", "")
    token = request.headers.get("x-scraper-token", "")
    if token != API_TOKEN and auth != f"Bearer {API_TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _sb_ready():
        return JSONResponse({"error": "Supabase niet geconfigureerd"}, status_code=500)
    rows = sb_select("tracked_products", {"select": "url,name", "limit": "200"})
    ts = _now() if _now else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    ok = 0
    errors = {}
    for row in rows:
        url = row["url"]
        try:
            price, name = _price_of(url)
            sb_upsert(
                "tracked_products",
                {"url": url, "name": name or row.get("name"), "current_price": price, "last_checked": ts},
                "url",
            )
            sb_insert("price_points", {"product_url": url, "price": price, "checked_at": ts})
            ok += 1
        except Exception as e:
            errors[url] = str(e)
    return JSONResponse({"checked": len(rows), "ok": ok, "errors": errors, "at": ts})
