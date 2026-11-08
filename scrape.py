"""
Lance une ou plusieurs recherches AutoScout24 et écrit les résultats dans docs/data/
(lus par la page GitHub Pages).

  python scrape.py                      -> toutes les recherches de config.json
  python scrape.py --make volkswagen --model polo --price-to 15000 --max-pages 35

Pour chaque recherche :
  - parcourt toutes les pages (jusqu'à --max-pages)
  - garde la date de première apparition de chaque annonce (pour repérer les nouvelles)
  - note les baisses de prix
  - envoie les nouvelles bonnes affaires sur Discord si DISCORD_WEBHOOK est défini
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import scraper

ROOT = Path(__file__).parent
DATA = ROOT / "docs" / "data"
FILTER_KEYS = ["make", "model", "country", "price_from", "price_to", "year_from", "year_to",
               "km_from", "km_to", "fuel", "gear", "zip", "radius", "sort"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def search_slug(s: dict) -> str:
    if s.get("name"):
        return scraper.slug(s["name"])
    parts = [s.get("make"), s.get("model"), s.get("price_to") and f"max{s['price_to']}",
             s.get("km_to") and f"km{s['km_to']}", s.get("year_from") and f"des{s['year_from']}",
             s.get("fuel"), s.get("zip") and f"cp{s['zip']}"]
    return scraper.slug("-".join(str(p) for p in parts if p)) or "recherche"


def search_label(s: dict) -> str:
    if s.get("name"):
        return s["name"]
    bits = [f"{(s.get('make') or 'Toutes marques').title()} {(s.get('model') or '').title()}".strip()]
    if s.get("price_to"):
        bits.append(f"≤ {int(s['price_to']):,} €".replace(",", " "))
    if s.get("km_to"):
        bits.append(f"≤ {int(s['km_to']):,} km".replace(",", " "))
    if s.get("year_from"):
        bits.append(f"dès {s['year_from']}")
    return " · ".join(bits)


# ---------------------------------------------------------------- tendance prix/km

def fit_line(cars: list[dict]):
    pts = [(c["km"], c["price"]) for c in cars if c.get("price") and c.get("km") is not None]
    if len(pts) < 5:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    b = sum((x - mx) * (y - my) for x, y in pts) / den if den else 0
    return my - b * mx, b


def add_deal_score(cars: list[dict]):
    f = fit_line(cars)
    for c in cars:
        c["delta"] = None
        if f and c.get("price") and c.get("km") is not None:
            pred = f[0] + f[1] * c["km"]
            if pred > 0:
                c["delta"] = round((c["price"] - pred) / pred, 4)
    return f


def is_deal(c: dict) -> bool:
    return (c.get("delta") is not None and c["delta"] <= -0.10) or c.get("price_label") in ("top-price", "good-price")


# ---------------------------------------------------------------- discord

def notify_discord(label: str, cars: list[dict]):
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not hook or not cars:
        return
    try:
        from curl_cffi import requests as http
    except ImportError:
        import requests as http  # type: ignore

    embeds = []
    for c in cars[:10]:
        km = f"{c['km']:,}".replace(",", " ") if c.get("km") is not None else "?"
        price = f"{c['price']:,}".replace(",", " ") if c.get("price") else "?"
        delta = f" · {round(c['delta'] * 100)} % vs tendance" if c.get("delta") is not None else ""
        e = {
            "title": f"{c.get('make', '')} {c.get('model', '')} — {price} €"[:256],
            "url": c.get("url"),
            "description": f"{c.get('version') or ''}\n{c.get('first_reg') or ''} · {km} km · {c.get('fuel') or ''}{delta}\n"
                           f"📍 {c.get('zip') or ''} {c.get('city') or ''} · {c.get('seller_name') or ''}"[:2000],
            "color": 0x0F8A5A,
        }
        if c.get("image"):
            e["thumbnail"] = {"url": c["image"]}
        embeds.append(e)
    body = {"content": f"🚗 **{len(cars)} nouvelle(s) bonne(s) affaire(s)** — {label}", "embeds": embeds}
    try:
        r = http.post(hook, json=body, timeout=15)
        print(f"  Discord : HTTP {r.status_code}")
    except Exception as e:
        print(f"  Discord : échec ({e})")


# ---------------------------------------------------------------- run

def run_search(search: dict, max_pages: int) -> dict:
    slug = search_slug(search)
    label = search_label(search)
    out_file = DATA / f"{slug}.json"
    print(f"\n=== {label}  [{slug}]")

    previous = {}
    if out_file.exists():
        try:
            previous = {c["id"]: c for c in json.loads(out_file.read_text("utf-8")).get("listings", [])}
        except (ValueError, KeyError):
            previous = {}

    params = {k: search.get(k) for k in FILTER_KEYS if search.get(k) not in (None, "")}
    cars, seen, total, pages, error, last_ok = [], set(), None, None, None, 0
    page = 1
    while page <= max_pages:
        try:
            res = scraper.scrape_page(params, page)
        except Exception as e:
            error = f"page {page} : {e}"
            print("  ERREUR", error)
            break
        if page == 1:
            total, pages = res["total"], res["pages"]
            print(f"  {total} résultats annoncés sur {pages} pages (source : {res['source']})")
        fresh = [c for c in res["listings"] if c.get("id") and c["id"] not in seen]
        seen.update(c["id"] for c in fresh)
        cars += fresh
        if res["listings"]:
            last_ok = page
        print(f"  page {page} : +{len(fresh)} (total {len(cars)})")
        if not res["listings"] or (pages and page >= pages):
            break
        page += 1
        time.sleep(random.uniform(0.5, 1.5))

    stamp = now_iso()
    for c in cars:
        old = previous.get(c["id"])
        c["first_seen"] = old.get("first_seen", stamp) if old else stamp
        c["is_new"] = bool(previous) and old is None  # au premier run, rien n'est "nouveau"
        if old and old.get("price") and c.get("price") and c["price"] < old["price"]:
            c["price_drop"] = old["price"] - c["price"]
        elif old and old.get("price_drop") and old.get("price") == c.get("price"):
            c["price_drop"] = old["price_drop"]
        else:
            c["price_drop"] = None

    add_deal_score(cars)

    if not cars and previous:
        print("  Aucune annonce récupérée : on garde les données précédentes.")
        data = json.loads(out_file.read_text("utf-8"))
        data["last_error"] = error or "aucune annonce récupérée"
        data["last_attempt"] = stamp
        out_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        return {"slug": slug, "label": label, "count": len(data.get("listings", [])), "updated": data.get("updated"), "error": data["last_error"]}

    data = {
        "slug": slug, "label": label, "search": search, "updated": stamp, "last_attempt": stamp,
        "total_announced": total, "pages_announced": pages, "pages_scraped": last_ok,
        "last_error": error, "url": scraper.build_url(params, 1), "listings": cars,
    }
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"  -> {len(cars)} annonces écrites dans {out_file.relative_to(ROOT)}")

    new_deals = [c for c in cars if c["is_new"] and is_deal(c)]
    new_deals.sort(key=lambda c: c["delta"] if c.get("delta") is not None else 0)
    notify_discord(label, new_deals)
    return {"slug": slug, "label": label, "count": len(cars), "updated": stamp, "error": error,
            "new": sum(c["is_new"] for c in cars)}


def write_index(results: list[dict]):
    idx_file = DATA / "index.json"
    idx = {}
    if idx_file.exists():
        try:
            idx = {s["slug"]: s for s in json.loads(idx_file.read_text("utf-8")).get("searches", [])}
        except (ValueError, KeyError):
            idx = {}
    for r in results:
        idx[r["slug"]] = r
    searches = sorted(idx.values(), key=lambda s: s.get("updated") or "", reverse=True)
    idx_file.write_text(json.dumps({"updated": now_iso(), "searches": searches}, ensure_ascii=False, indent=1), "utf-8")


def main():
    ap = argparse.ArgumentParser()
    for k in FILTER_KEYS:
        ap.add_argument("--" + k.replace("_", "-"))
    ap.add_argument("--name")
    ap.add_argument("--max-pages", type=int, default=None)
    a = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
    max_pages = a.max_pages or cfg.get("max_pages", 20)
    scraper.MIN_DELAY = float(cfg.get("delay_seconds", scraper.MIN_DELAY))

    cli = {k: getattr(a, k) for k in FILTER_KEYS + ["name"] if getattr(a, k)}
    searches = [cli] if cli.get("make") or cli.get("model") or cli.get("name") else cfg.get("searches", [])
    if not searches:
        sys.exit("Aucune recherche : passe --make/--model ou remplis config.json")

    DATA.mkdir(parents=True, exist_ok=True)
    results = [run_search(s, max_pages) for s in searches]
    write_index(results)

    if all(r.get("error") and not r.get("count") for r in results):
        sys.exit("Toutes les recherches ont échoué (voir les erreurs ci-dessus).")


if __name__ == "__main__":
    main()
