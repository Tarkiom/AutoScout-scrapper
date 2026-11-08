"""
Scraper AutoScout24 — récupère une page de résultats et renvoie une liste d'annonces propres.

Stratégie :
  1. Lire le JSON Next.js (<script id="__NEXT_DATA__">) -> données riches (photos, version, vendeur...)
  2. Fallback : lire les attributs data-* des <article> si le JSON change de structure.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import time
from urllib.parse import urlencode

from bs4 import BeautifulSoup

try:  # curl_cffi imite l'empreinte TLS de Chrome -> beaucoup moins de blocages
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # fallback si curl_cffi ne s'installe pas
    import requests as http  # type: ignore
    IMPERSONATE = {}

BASE = "https://www.autoscout24.be/fr/lst"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
}

SORTS = {
    "standard": ("standard", 0),
    "prix_asc": ("price", 0),
    "prix_desc": ("price", 1),
    "km_asc": ("mileage", 0),
    "annee_desc": ("year", 1),
    "recent": ("age", 1),
}

_last_request = 0.0
MIN_DELAY = 1.5  # secondes entre deux requêtes, pour rester discret


def slug(s: str | None) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def build_url(params: dict, page: int) -> str:
    make, model = slug(params.get("make")), slug(params.get("model"))
    path = BASE
    if make:
        path += f"/{make}"
        if model:
            path += f"/{model}"

    sort, desc = SORTS.get(params.get("sort") or "standard", SORTS["standard"])
    q = {
        "atype": "C",
        "cy": params.get("country") or "B",
        "sort": sort,
        "desc": desc,
        "page": page,
        "ustate": "N,U",
    }
    mapping = {
        "price_from": "pricefrom",
        "price_to": "priceto",
        "year_from": "fregfrom",
        "year_to": "fregto",
        "km_from": "kmfrom",
        "km_to": "kmto",
        "fuel": "fuel",
        "gear": "gear",
        "zip": "zip",
        "radius": "zipr",
    }
    for key, as_key in mapping.items():
        v = params.get(key)
        if v not in (None, "", "0"):
            q[as_key] = v
    return f"{path}?{urlencode(q)}"


class ScrapeError(Exception):
    pass


def fetch(url: str) -> str:
    global _last_request
    wait = MIN_DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()

    r = http.get(url, headers=HEADERS, timeout=20, **IMPERSONATE)
    if r.status_code == 403:
        raise ScrapeError("403 : AutoScout24 bloque la requête (anti-bot). Attends un peu ou change d'IP.")
    if r.status_code == 429:
        raise ScrapeError("429 : trop de requêtes. Augmente le délai et réessaie plus tard.")
    if r.status_code >= 400:
        raise ScrapeError(f"HTTP {r.status_code} sur {url}")
    return r.text


# ---------------------------------------------------------------- parsing

def _to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None


def _year(v) -> int | None:
    """'03-2018' / '2018' / '2018-03' -> 2018"""
    if not v:
        return None
    m = re.search(r"(19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


def _abs_url(u: str | None) -> str | None:
    if not u:
        return None
    return u if u.startswith("http") else "https://www.autoscout24.be" + u


def _power(details) -> str | None:
    """vehicleDetails = [{iconName, ariaLabel, data}, ...] -> '110 kW (150 CV)'"""
    if not isinstance(details, list):
        return None
    for d in details:
        label = f"{d.get('iconName', '')} {d.get('ariaLabel', '')}".lower()
        if "speed" in label or "puissance" in label or "leistung" in label:
            return d.get("data")
    return None


def _from_next_data(html: str) -> tuple[list[dict], int | None, int | None] | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        raw_json = m.group(1).strip()
        if raw_json.startswith("{&") or "&#34;" in raw_json[:200]:
            raw_json = htmllib.unescape(raw_json)  # certaines pages encodent le JSON en entités HTML
        data = json.loads(raw_json)
        props = data["props"]["pageProps"]
    except (ValueError, KeyError):
        return None

    raw = props.get("listings") or []
    total = _to_int(props.get("numberOfResults"))
    pages = _to_int(props.get("numberOfPages"))

    out = []
    for l in raw:
        v = l.get("vehicle") or {}
        t = l.get("tracking") or {}
        loc = l.get("location") or {}
        price = l.get("price") or {}
        seller = l.get("seller") or {}
        imgs = l.get("images") or []

        out.append({
            "id": l.get("id"),
            "url": _abs_url(l.get("url")),
            "make": v.get("make"),
            "model": v.get("model"),
            "version": v.get("modelVersionInput") or v.get("variant"),
            "subtitle": v.get("subtitle"),
            "price": _to_int(price.get("priceRaw")) or _to_int(t.get("price")) or _to_int(price.get("priceFormatted")),
            "price_label": t.get("priceLabel"),
            "km": _to_int(t.get("mileage")) or _to_int(v.get("mileageInKm")),
            "year": _year(t.get("firstRegistration")),
            "first_reg": t.get("firstRegistration"),
            "fuel": v.get("fuel"),
            "gear": v.get("transmission"),
            "power": _power(l.get("vehicleDetails")),
            "city": loc.get("city"),
            "zip": loc.get("zip"),
            "country": loc.get("countryCode"),
            "seller": seller.get("type"),
            "seller_name": seller.get("companyName") or seller.get("contactName"),
            "image": imgs[0] if imgs else None,
            "image_count": len(imgs),
        })
    return out, total, pages


def _from_articles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("article[data-guid]"):
        link = a.select_one("a[href*='/offres/'], a[href*='/angebote/'], a[href*='/aanbod/']")
        img = a.select_one("img[src]")
        out.append({
            "id": a.get("data-guid"),
            "url": _abs_url(link.get("href")) if link else None,
            "make": a.get("data-make"),
            "model": a.get("data-model"),
            "version": None,
            "price": _to_int(a.get("data-price")),
            "price_label": a.get("data-price-label"),
            "km": _to_int(a.get("data-mileage")),
            "year": _year(a.get("data-first-registration")),
            "first_reg": a.get("data-first-registration"),
            "fuel": a.get("data-fuel-type"),
            "gear": None,
            "power": None,
            "city": None,
            "zip": a.get("data-listing-zip-code"),
            "country": None,
            "seller": a.get("data-seller-type"),
            "seller_name": None,
            "image": img.get("src") if img else None,
            "image_count": None,
        })
    return out


def parse(html: str) -> dict:
    nd = _from_next_data(html)
    if nd and nd[0]:
        listings, total, pages = nd
        source = "next_data"
    else:
        listings, total, pages = _from_articles(html), None, None
        source = "html"
    return {"listings": listings, "total": total, "pages": pages, "source": source}


def scrape_page(params: dict, page: int) -> dict:
    url = build_url(params, page)
    result = parse(fetch(url))
    result["url"] = url
    result["page"] = page
    return result


# ---------------------------------------------------------------- page d'une annonce

def _find_texts(o, key="description", out=None):
    out = [] if out is None else out
    if isinstance(o, dict):
        for k, v in o.items():
            if k.lower() == key and isinstance(v, str):
                out.append(v)
            else:
                _find_texts(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _find_texts(v, key, out)
    return out


def fetch_description(url: str) -> str | None:
    """Renvoie le texte de description d'une annonce (sans HTML), ou None."""
    html = fetch(url)
    texts = []
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        raw = m.group(1).strip()
        if raw.startswith("{&") or "&#34;" in raw[:200]:
            raw = htmllib.unescape(raw)
        try:
            texts = _find_texts(json.loads(raw))
        except ValueError:
            texts = []
    if not texts:  # repli : bloc description dans le HTML
        m = re.search(r'data-testid="[^"]*description[^"]*"[^>]*>(.*?)</div>', html, re.S | re.I)
        if m:
            texts = [m.group(1)]
    if not texts:
        return None
    text = max(texts, key=len)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", text).strip() or None
