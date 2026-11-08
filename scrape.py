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


def _solve(m: list[list[float]], v: list[float]):
    """Résout m·x = v (élimination de Gauss avec pivot)."""
    n = len(v)
    m = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _kw(power) -> float | None:
    m = re.search(r"(\d+)\s*kW", str(power or ""))
    return float(m.group(1)) if m else None


def _features(c: dict, year_now: int, kw_default: float, full: bool) -> list[float]:
    age = max(0, year_now - c["year"])
    base = [1.0, age, c["km"] / 10000]
    if not full:
        return base
    fuel = (c.get("fuel") or "").lower()
    gear = (c.get("gear") or "").lower()
    kw = _kw(c.get("power")) or kw_default
    return base + [
        kw / 10,
        1.0 if "diesel" in fuel else 0.0,
        1.0 if ("/" in fuel or "hybr" in fuel) else 0.0,
        1.0 if ("lectri" in fuel and "/" not in fuel) else 0.0,
        1.0 if "auto" in gear else 0.0,
        1.0 if (c.get("seller") or "").lower().startswith("d") else 0.0,
    ]


FEATURE_NAMES = ["âge", "km", "puissance", "diesel", "hybride", "électrique", "boîte auto", "vendeur pro"]


def price_model(cars: list[dict]):
    """ln(prix) ~ âge + km (+ puissance, carburant, boîte, type de vendeur si assez d'annonces).
    Régression ridge légère. Renvoie (predict, infos) ou (None, None)."""
    import math
    year_now = datetime.now().year
    pool = [c for c in cars if c.get("price") and c["price"] >= MIN_PRICE and c.get("km") is not None
            and c.get("year") and not c.get("suspicious")]
    if len(pool) < 10:
        return None, None
    kws = sorted(k for k in (_kw(c.get("power")) for c in pool) if k)
    kw_default = kws[len(kws) // 2] if kws else 70.0
    full = len(pool) >= 40
    X = [_features(c, year_now, kw_default, full) for c in pool]
    y = [math.log(c["price"]) for c in pool]
    k = len(X[0])
    xtx = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yy for r, yy in zip(X, y)) for i in range(k)]
    for i in range(1, k):                      # ridge : évite les colonnes vides (ex. aucun diesel)
        xtx[i][i] += 0.5
    coef = _solve(xtx, xty)
    if not coef:
        return None, None
    pred = [sum(a * b for a, b in zip(coef, r)) for r in X]
    my = sum(y) / len(y)
    ss_tot = sum((yy - my) ** 2 for yy in y) or 1
    r2 = 1 - sum((yy - p) ** 2 for yy, p in zip(y, pred)) / ss_tot
    names = FEATURE_NAMES[:k - 1]
    infos = {"n": len(pool), "r2": round(r2, 3), "features": names,
             "effects": {n: round(math.exp(cf) - 1, 4) for n, cf in zip(names, coef[1:])}}

    def predict(c):
        if not (c.get("km") is not None and c.get("year")):
            return None
        return math.exp(sum(a * b for a, b in zip(coef, _features(c, year_now, kw_default, full))))
    return predict, infos


def flag_suspicious(c: dict) -> bool:
    text = f"{c.get('version') or ''} {c.get('subtitle') or ''}"
    return (c.get("price_label") == "toolow-price"
            or (bool(c.get("price")) and c["price"] < MIN_PRICE)
            or bool(RED_FLAGS.search(text)))


def add_deal_score(cars: list[dict]):
    for c in cars:
        c["price_label"] = (c.get("price_label") or "").strip() or None
        c["suspicious"] = flag_suspicious(c)
    predict, infos = price_model(cars)
    for c in cars:
        c["expected_price"] = None
        c["delta"] = None
        if predict and c.get("price") and c["price"] >= MIN_PRICE:
            exp = predict(c)
            if exp and exp > 0:
                c["expected_price"] = round(exp, -1)
                c["delta"] = round(c["price"] / exp - 1, 4)
    return infos


def is_deal(c: dict) -> bool:
    if c.get("suspicious"):
        return False
    return (c.get("delta") is not None and c["delta"] <= DEAL_THRESHOLD) or c.get("price_label") == "top-price"


# ---------------------------------------------------------------- alertes

def _http():
    try:
        from curl_cffi import requests as http
    except ImportError:
        import requests as http  # type: ignore
    return http


def _n(v) -> str:
    return f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else "?"


def _line(c: dict) -> str:
    delta = f" · {round(c['delta'] * 100)} % vs attendu" if c.get("delta") is not None else ""
    return f"{c.get('first_reg') or c.get('year') or ''} · {_n(c.get('km'))} km · {c.get('fuel') or ''}{delta}"


def notify(label: str, deals: list[dict], drops: list[dict], page_url: str | None):
    """Envoie les alertes : nouvelles bonnes affaires + baisses de prix sur des bonnes affaires."""
    if not deals and not drops:
        return
    http = _http()

    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if hook:
        embeds = []
        for kind, c in [("new", c) for c in deals[:7]] + [("drop", c) for c in drops[:3]]:
            title = f"{c.get('make', '')} {c.get('model', '')} — {_n(c.get('price'))} €"
            if kind == "drop":
                title = f"📉 −{_n(c.get('price_drop'))} € · " + title
            e = {"title": title[:256], "url": c.get("url"), "color": 0x0F8A5A if kind == "new" else 0xFFB020,
                 "description": f"{c.get('version') or ''}\n{_line(c)}\n📍 {c.get('zip') or ''} {c.get('city') or ''} · {c.get('seller_name') or ''}"[:2000]}
            if c.get("image"):
                e["thumbnail"] = {"url": c["image"]}
            embeds.append(e)
        parts = []
        if deals:
            parts.append(f"{len(deals)} nouvelle(s) bonne(s) affaire(s)")
        if drops:
            parts.append(f"{len(drops)} baisse(s) de prix")
        try:
            r = http.post(hook, json={"content": f"🚗 **{' · '.join(parts)}** — {label}", "embeds": embeds}, timeout=15)
            print(f"  Discord : HTTP {r.status_code}")
        except Exception as e:
            print(f"  Discord : échec ({e})")

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        msgs = []
        for c in deals[:5]:
            msgs.append(("🚗 " + f"{c.get('make', '')} {c.get('model', '')} à {_n(c.get('price'))} €", _line(c), c))
        for c in drops[:3]:
            msgs.append(("📉 " + f"−{_n(c.get('price_drop'))} € : {c.get('make', '')} {c.get('model', '')} à {_n(c.get('price'))} €", _line(c), c))
        extra = len(deals) + len(drops) - len(msgs)
        for i, (title, body, c) in enumerate(msgs):
            if i == len(msgs) - 1 and extra > 0:
                body += f"\n+ {extra} autre(s) sur le site"
            headers = {"Title": title.encode("utf-8"), "Tags": "car", "Priority": "high" if (c.get("delta") or 0) <= -0.25 else "default"}
            if c.get("url"):
                headers["Click"] = c["url"]
            if c.get("image"):
                headers["Attach"] = c["image"]
            if page_url:
                headers["Actions"] = f"view, Voir sur le radar, {page_url}"
            try:
                r = http.post(f"https://ntfy.sh/{topic}", data=f"{label}\n{body}".encode("utf-8"), headers=headers, timeout=15)
                print(f"  ntfy : HTTP {r.status_code}")
            except Exception as e:
                print(f"  ntfy : échec ({e})")


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
    today = stamp[:10]
    complete = error is None and bool(cars) and (not pages or last_ok >= pages)

    for c in cars:
        old = previous.get(c["id"])
        c["first_seen"] = old.get("first_seen", stamp) if old else stamp
        c["is_new"] = bool(previous) and old is None  # au premier run, rien n'est "nouveau"
        hist = list(old.get("price_history") or []) if old else []
        if old and not hist and old.get("price"):
            hist = [[old["first_seen"][:10], old["price"]]]
        if c.get("price") and (not hist or hist[-1][1] != c["price"]):
            hist.append([today, c["price"]])
        c["price_history"] = hist[-30:]
        first_price = hist[0][1] if hist else None
        c["price_drop"] = first_price - c["price"] if first_price and c.get("price") and c["price"] < first_price else None
        c["dropped_now"] = bool(old and old.get("price") and c.get("price") and c["price"] < old["price"])

    model = add_deal_score(cars)

    prev_data = {}
    if out_file.exists():
        try:
            prev_data = json.loads(out_file.read_text("utf-8"))
        except ValueError:
            prev_data = {}

    if not cars and previous:
        print("  Aucune annonce récupérée : on garde les données précédentes.")
        prev_data["last_error"] = error or "aucune annonce récupérée"
        prev_data["last_attempt"] = stamp
        out_file.write_text(json.dumps(prev_data, ensure_ascii=False, indent=1), "utf-8")
        return {"slug": slug, "label": label, "count": len(prev_data.get("listings", [])), "updated": prev_data.get("updated"), "error": prev_data["last_error"]}

    # Annonces disparues (vendues ou retirées) : seulement si on a vu toutes les pages
    sold = list(prev_data.get("sold") or [])
    if complete and previous:
        current_ids = {c["id"] for c in cars}
        gone = [c for cid, c in previous.items() if cid not in current_ids]
        for c in gone:
            hist = c.get("price_history") or ([[c.get("first_seen", stamp)[:10], c.get("price")]] if c.get("price") else [])
            days = (datetime.fromisoformat(stamp) - datetime.fromisoformat(c.get("first_seen", stamp))).days
            sold.insert(0, {k: c.get(k) for k in ("id", "url", "make", "model", "version", "year", "first_reg", "km", "fuel",
                                                  "gear", "power", "zip", "city", "seller_name", "image", "delta", "expected_price", "suspicious")}
                        | {"price": c.get("price"), "first_price": hist[0][1] if hist else c.get("price"),
                           "first_seen": c.get("first_seen"), "gone_at": stamp, "days_online": max(days, 0)})
        if gone:
            print(f"  {len(gone)} annonce(s) disparue(s) depuis le dernier passage")
    sold = sold[:400]

    # Suivi du marché : un point par jour
    ok = sorted(c["price"] for c in cars if c.get("price") and not c.get("suspicious"))
    kms = sorted(c["km"] for c in cars if c.get("km") is not None and not c.get("suspicious"))
    q = lambda arr, f: arr[min(len(arr) - 1, int(f * len(arr)))] if arr else None
    point = {"date": today, "count": len(cars), "median": q(ok, .5), "p25": q(ok, .25), "p75": q(ok, .75),
             "median_km": q(kms, .5), "deals": sum(1 for c in cars if is_deal(c)), "gone": sum(1 for s_ in sold if (s_.get("gone_at") or "")[:10] == today)}
    market = [m for m in (prev_data.get("market") or []) if m.get("date") != today] + [point]
    market = market[-365:]

    data = {
        "slug": slug, "label": label, "search": search, "updated": stamp, "last_attempt": stamp,
        "total_announced": total, "pages_announced": pages, "pages_scraped": last_ok, "complete": complete,
        "last_error": error, "url": scraper.build_url(params, 1), "model": model,
        "market": market, "sold": sold, "listings": cars,
    }
    out_file.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"  -> {len(cars)} annonces écrites dans {out_file.relative_to(ROOT)}"
          + (f" (modèle de prix : {model['n']} annonces, R² {model['r2']})" if model else ""))

    new_deals = sorted([c for c in cars if c["is_new"] and is_deal(c)], key=lambda c: c["delta"] if c.get("delta") is not None else 0)
    drops = sorted([c for c in cars if c["dropped_now"] and not c["suspicious"] and (is_deal(c) or (c.get("delta") or 1) <= -0.05)],
                   key=lambda c: -(c.get("price_drop") or 0))
    owner, repo = (os.environ.get("GITHUB_REPOSITORY") or "/").split("/")
    page_url = f"https://{owner.lower()}.github.io/{repo}/#{slug}" if owner else None
    notify(label, new_deals, drops, page_url)
    return {"slug": slug, "label": label, "count": len(cars), "updated": stamp, "error": error,
            "new": sum(c["is_new"] for c in cars), "deals": sum(1 for c in cars if is_deal(c))}


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
        # Modification : on remplace l'ancienne entrée. Nouvelle recherche : on ne retire jamais une surveillance existante.
        drop = ({new, replace} - {None}) if (replace or a.watch) else set()
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
