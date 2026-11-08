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
import re
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
             s.get("fuel"), s.get("gear"), s.get("zip") and f"cp{s['zip']}", s.get("radius") and f"r{s['radius']}"]
    return scraper.slug("-".join(str(p) for p in parts if p)) or "recherche"


def search_label(s: dict) -> str:
    if s.get("name"):
        return s["name"]
    bits = [f"{(s.get('make') or 'Toutes marques').replace('-', ' ').title()} {(s.get('model') or '').replace('-', ' ').title()}".strip()]
    if s.get("price_to"):
        bits.append(f"≤ {int(s['price_to']):,} €".replace(",", " "))
    if s.get("km_to"):
        bits.append(f"≤ {int(s['km_to']):,} km".replace(",", " "))
    if s.get("year_from"):
        bits.append(f"dès {s['year_from']}")
    fuels = {"B": "essence", "D": "diesel", "E": "électrique", "2": "hybride", "L": "GPL", "C": "CNG"}
    if s.get("fuel"):
        bits.append(fuels.get(s["fuel"], s["fuel"]))
    if s.get("gear"):
        bits.append({"M": "manuelle", "A": "automatique"}.get(s["gear"], s["gear"]))
    if s.get("zip"):
        bits.append(f"{s.get('radius') or '?'} km autour de {s['zip']}" if s.get("radius") else f"CP {s['zip']}")
    return " · ".join(bits)


# ---------------------------------------------------------------- tendance prix/km

MIN_PRICE = 1500          # en dessous : épave, pièces ou arnaque -> exclu du calcul
RED_FLAGS = re.compile(
    r"export|marchand|n[ée]gociant|pi[eè]ces|panne|accident|sinistr|d[ée]fect|ne (d[ée]marre|roule)|"
    r"probl[eè]me|moteur hs|hs\b|bo[iî]te hs|[ée]pave|pour bricoleur|sans ct|non roulant|r[ée]paration",
    re.I)
DEAL_THRESHOLD = -0.15    # 15 % sous le prix attendu


def _solve3(m, v):
    """Résout un système 3x3 (élimination de Gauss)."""
    m = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][3] / m[i][i] for i in range(3)]


def price_model(cars: list[dict]):
    """ln(prix) = a + b*âge + c*km  (moindres carrés). Renvoie predict(car) -> prix attendu."""
    import math
    year_now = datetime.now().year
    pts = [(year_now - c["year"], c["km"] / 10000, math.log(c["price"])) for c in cars
           if c.get("price") and c["price"] >= MIN_PRICE and c.get("km") is not None and c.get("year")]
    if len(pts) < 10:
        return None
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for age, km, y in pts:
        x = (1.0, age, km)
        for i in range(3):
            xty[i] += x[i] * y
            for j in range(3):
                xtx[i][j] += x[i] * x[j]
    coef = _solve3(xtx, xty)
    if not coef:
        return None
    a, b, c = coef
    return lambda car: math.exp(a + b * (year_now - car["year"]) + c * car["km"] / 10000)


def add_deal_score(cars: list[dict]):
    predict = price_model(cars)
    for c in cars:
        c["price_label"] = (c.get("price_label") or "").strip() or None
        c["expected_price"] = None
        c["delta"] = None
        if predict and c.get("price") and c["price"] >= MIN_PRICE and c.get("km") is not None and c.get("year"):
            exp = predict(c)
            if exp > 0:
                c["expected_price"] = round(exp, -1)
                c["delta"] = round(c["price"] / exp - 1, 4)
        text = f"{c.get('version') or ''} {c.get('subtitle') or ''}"
        c["suspicious"] = (c.get("price_label") == "toolow-price"
                           or (bool(c.get("price")) and c["price"] < MIN_PRICE)
                           or bool(RED_FLAGS.search(text)))


def is_deal(c: dict) -> bool:
    if c.get("suspicious"):
        return False
    return (c.get("delta") is not None and c["delta"] <= DEAL_THRESHOLD) or c.get("price_label") == "top-price"


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


def write_index(results: list[dict], watched: set[str], removed: set[str] = frozenset()):
    idx_file = DATA / "index.json"
    idx = {}
    if idx_file.exists():
        try:
            idx = {s["slug"]: s for s in json.loads(idx_file.read_text("utf-8")).get("searches", [])}
        except (ValueError, KeyError):
            idx = {}
    for slug in removed:
        idx.pop(slug, None)
    for r in results:
        idx[r["slug"]] = r
    for slug, entry in idx.items():
        entry["watched"] = slug in watched
    searches = sorted(idx.values(), key=lambda s: s.get("updated") or "", reverse=True)
    idx_file.write_text(json.dumps({"updated": now_iso(), "searches": searches}, ensure_ascii=False, indent=1), "utf-8")


def save_config(cfg: dict):
    (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")


def remove_data(slug: str):
    f = DATA / f"{slug}.json"
    if f.exists():
        f.unlink()
        print(f"  Données supprimées : {f.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    for k in FILTER_KEYS:
        ap.add_argument("--" + k.replace("_", "-"))
    ap.add_argument("--name")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--search-json", help="recherche complète en JSON (envoyée par le site)")
    ap.add_argument("--watch", action="store_true", help="ajoute la recherche à config.json (scraping toutes les 6 h)")
    a = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
    cfg.setdefault("searches", [])
    max_pages = a.max_pages or cfg.get("max_pages", 20)
    scraper.MIN_DELAY = float(cfg.get("delay_seconds", scraper.MIN_DELAY))
    DATA.mkdir(parents=True, exist_ok=True)

    cli = {k: getattr(a, k) for k in FILTER_KEYS + ["name"] if getattr(a, k)}
    replace = delete = None
    from_site = False
    if a.search_json:
        try:
            raw = json.loads(a.search_json)
        except ValueError:
            sys.exit("search_json invalide")
        from_site = True
        replace = scraper.slug(raw.get("_replace") or "") or None
        delete = scraper.slug(raw.get("_delete") or "") or None
        cli = {k: str(v).strip() for k, v in raw.items() if k in FILTER_KEYS + ["name"] and str(v or "").strip()}

    watched = lambda: {search_slug(s) for s in cfg["searches"]}

    # --- suppression d'une recherche
    if delete:
        before = len(cfg["searches"])
        cfg["searches"] = [s for s in cfg["searches"] if search_slug(s) != delete]
        if len(cfg["searches"]) != before:
            save_config(cfg)
            print(f"Surveillance arrêtée : {delete}")
        remove_data(delete)
        write_index([], watched(), removed={delete})
        print(f"Recherche supprimée : {delete}")
        return

    # --- surveillance (ajout / retrait), y compris lors d'une modification
    if cli and from_site:
        new = search_slug(cli)
        drop = {new, replace} - {None}
        kept = [s for s in cfg["searches"] if search_slug(s) not in drop]
        if a.watch:
            kept.append(cli)
        if [search_slug(s) for s in kept] != [search_slug(s) for s in cfg["searches"]] or a.watch:
            cfg["searches"] = kept
            save_config(cfg)
            print(("Surveillée toutes les 6 h : " if a.watch else "Non surveillée : ") + search_label(cli))
    elif cli and a.watch and search_slug(cli) not in watched():
        cfg["searches"].append(cli)
        save_config(cfg)

    searches = [cli] if cli else cfg["searches"]
    if not searches:
        sys.exit("Aucune recherche : passe --make/--model ou remplis config.json")

    results = [run_search(s, max_pages) for s in searches]

    removed = set()
    if replace and cli and search_slug(cli) != replace:
        remove_data(replace)
        removed.add(replace)
        print(f"Ancienne recherche remplacée : {replace} -> {search_slug(cli)}")
    write_index(results, watched(), removed)

    if all(r.get("error") and not r.get("count") for r in results):
        sys.exit("Toutes les recherches ont échoué (voir les erreurs ci-dessus).")


if __name__ == "__main__":
    main()
