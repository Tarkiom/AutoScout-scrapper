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
    r"probl[eè]me?|probleem|moteur hs|hs\b|bo[iî]te hs|[ée]pave|pour bricoleur|non roulant|r[ée]paration|"
    r"bloqu[ée]|[àa] r[ée]viser|voyant|"
    # néerlandais
    r"handelaars?|handelaren|opkopers?|onderdelen|kapot|defect|motorprobleem|motorschade|schade|ongeval|"
    r"start niet|rijdt niet|niet rijdend|koppakking|"
    # anglais
    r"dealers? only|trade only|for parts|spares|non[- ]runner|not running|engine (problem|failure|damage)|damaged",
    re.I)
DEAL_THRESHOLD = -0.15    # 15 % sous le prix attendu
REF_CAP_FACTOR = 2.5      # référence : plafond de prix × 2,5
REF_MAX_PAGES = 40        # référence : 800 annonces au maximum
TOO_GOOD = -0.50          # plus de 50 % sous le prix attendu : trop beau pour être vrai
DESC_CHECKS_PER_RUN = 90  # pages d'annonces ouvertes au maximum par passage
MAKES = ["abarth", "alfa romeo", "audi", "bmw", "citroen", "citroën", "cupra", "dacia", "ds", "fiat", "ford", "honda", "hyundai",
         "jaguar", "jeep", "kia", "land rover", "lexus", "mazda", "mercedes", "mini", "mitsubishi", "nissan", "opel", "peugeot",
         "porsche", "renault", "seat", "skoda", "smart", "subaru", "suzuki", "tesla", "toyota", "volkswagen", "volvo"]
MAKE_ALIASES = {"volkswagen": ["vw"], "mercedes-benz": ["mercedes", "merco", "benz"], "citroen": ["citroën"], "alfa romeo": ["alfa"],
                "land rover": ["range rover", "landrover"], "bmw": ["bm"]}


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


def _make_key(c: dict) -> str:
    return scraper.slug(c.get("make") or "")


def _model_key(c: dict) -> str:
    return f"{_make_key(c)}|{scraper.slug(c.get('model') or '')}"


def _fit(X: list[list[float]], y: list[float], pen: list[float]):
    k = len(X[0])
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r, yy in zip(X, y):
        nz = [(i, v) for i, v in enumerate(r) if v]
        for i, vi in nz:
            xty[i] += vi * yy
            row = xtx[i]
            for j, vj in nz:
                row[j] += vi * vj
    for i in range(k):
        xtx[i][i] += pen[i]
    return _solve(xtx, xty)


def price_model(cars: list[dict]):
    """ln(prix) ~ âge + km + puissance + carburant + boîte + vendeur + marque + modèle.

    Régression ridge : la marque et le modèle sont des effets « rétrécis ». Un modèle avec peu d'annonces
    reste proche de sa marque, une marque rare reste proche de la moyenne générale.
    Le prix attendu de chaque annonce est calculé sans elle (validation croisée), pour qu'une annonce
    ne tire pas sa propre estimation vers son prix. Renvoie (predict, infos, oof) ou (None, None, {})."""
    import math
    import random as rnd
    from collections import Counter
    year_now = datetime.now().year
    pool = [c for c in cars if c.get("price") and c["price"] >= MIN_PRICE and c.get("km") is not None
            and c.get("year") and not c.get("suspicious")]
    if len(pool) < 10:
        return None, None, {}
    kws = sorted(k for k in (_kw(c.get("power")) for c in pool) if k)
    kw_default = kws[len(kws) // 2] if kws else 70.0
    full = len(pool) >= 40

    make_n = Counter(_make_key(c) for c in pool)
    model_n = Counter(_model_key(c) for c in pool)
    makes = sorted(m for m, n in make_n.items() if n >= 3 and m) if len(make_n) > 1 else []
    models = sorted(m for m, n in model_n.items() if n >= 3 and not m.endswith("|")) if len(model_n) > 1 else []
    mi = {m: i for i, m in enumerate(makes)}
    di = {m: i for i, m in enumerate(models)}
    n_base = len(_features(pool[0], year_now, kw_default, full))

    def row(c):
        r = _features(c, year_now, kw_default, full) + [0.0] * (len(makes) + len(models))
        j = mi.get(_make_key(c))
        if j is not None:
            r[n_base + j] = 1.0
        j = di.get(_model_key(c))
        if j is not None:
            r[n_base + len(makes) + j] = 1.0
        return r

    X = [row(c) for c in pool]
    y = [math.log(c["price"]) for c in pool]
    pen = [0.0] + [0.5] * (n_base - 1) + [3.0] * len(makes) + [4.0] * len(models)
    coef = _fit(X, y, pen)
    if not coef:
        return None, None, {}

    # validation croisée 5 blocs : prix attendu "hors échantillon" + précision honnête
    idx = list(range(len(pool)))
    rnd.Random(42).shuffle(idx)
    folds = [idx[f::5] for f in range(5)]
    oof_pred = [None] * len(pool)
    for f in folds:
        test = set(f)
        tr = [i for i in idx if i not in test]
        cf = _fit([X[i] for i in tr], [y[i] for i in tr], pen) or coef
        for i in f:
            oof_pred[i] = sum(a * b for a, b in zip(cf, X[i]))
    my = sum(y) / len(y)
    ss_tot = sum((yy - my) ** 2 for yy in y) or 1
    r2 = 1 - sum((yy - p) ** 2 for yy, p in zip(y, oof_pred)) / ss_tot

    names = FEATURE_NAMES[:n_base - 1]
    infos = {"n": len(pool), "r2": round(max(r2, 0), 3), "features": names,
             "effects": {n: round(math.exp(cf) - 1, 4) for n, cf in zip(names, coef[1:n_base])},
             "makes": len(makes), "models": len(models)}
    oof = {c["id"]: math.exp(p) for c, p in zip(pool, oof_pred)}

    def predict(c):
        if not (c.get("km") is not None and c.get("year")):
            return None
        return math.exp(sum(a * b for a, b in zip(coef, row(c))))
    return predict, infos, oof


# Motorisations et boîtes connues pour des faiblesses récurrentes (points à vérifier, pas une condamnation).
PSA = ("peugeot", "citroen", "ds", "opel")
VAG = ("volkswagen", "audi", "seat", "skoda", "cupra")
ENGINE_RISKS = [
    {"key": "puretech", "label": "PureTech 1.0 / 1.2",
     "tip": "Courroie de distribution qui baigne dans l'huile et se désagrège, consommation d'huile. Demande la facture du remplacement de courroie et vérifie le niveau d'huile.",
     "test": lambda c, t, f: c["make_k"] in PSA and ("puretech" in t or (re.search(r"\b1[.,]2\b", t) and "essence" in f and c["make_k"] != "opel")) and 2013 <= c["year"] <= 2022},
    {"key": "ecoboost", "label": "1.0 EcoBoost",
     "tip": "Courroie humide et risque de surchauffe (fuite de liquide de refroidissement). Vérifie l'historique d'entretien et l'absence de perte de liquide.",
     "test": lambda c, t, f: c["make_k"] == "ford" and ("ecoboost" in t or (re.search(r"\b1[.,]0\b", t) and "essence" in f)) and 2012 <= c["year"] <= 2019},
    {"key": "tsi-ea111", "label": "1.2 / 1.4 TSI (1re génération)",
     "tip": "Chaîne de distribution qui s'allonge (bruit de ferraille au démarrage à froid). Écoute le moteur à froid, demande si la chaîne a été changée.",
     "test": lambda c, t, f: c["make_k"] in VAG and re.search(r"\b1[.,][24]\s*t?fsi|\b1[.,][24]\s*tsi", t) and c["year"] <= 2012},
    {"key": "tce-h5ft", "label": "1.2 TCe / DIG-T (2012-2016)",
     "tip": "Moteur H5Ft connu pour sa consommation d'huile qui peut aller jusqu'à la casse. Vérifie le niveau d'huile, demande si la mise à jour Renault a été faite et l'historique d'entretien.",
     "test": lambda c, t, f: _h5ft(c, t) and 2012 <= c["year"] <= 2016},
    {"key": "tce-h5ft-late", "label": "1.2 TCe / DIG-T (2017-2019)",
     "tip": "Même moteur H5Ft : Renault a corrigé une partie du problème fin 2016, le risque est plus faible mais pas nul. Vérifie le niveau d'huile à froid et demande l'historique d'entretien.",
     "test": lambda c, t, f: _h5ft(c, t) and 2017 <= c["year"] <= 2019},
    {"key": "thp", "label": "1.6 THP / Mini Cooper S",
     "tip": "Chaîne de distribution et consommation d'huile (moteur « Prince »). Écoute le démarrage à froid et vérifie l'entretien.",
     "test": lambda c, t, f: (("thp" in t and c["make_k"] in PSA) or (c["make_k"] == "mini" and "cooper s" in t)) and c["year"] <= 2015},
    {"key": "n47", "label": "BMW 2.0 diesel (N47)",
     "tip": "Chaîne de distribution côté boîte, coûteuse à remplacer. Écoute un sifflement / cliquetis moteur, demande si elle a été changée.",
     "test": lambda c, t, f: c["make_k"] == "bmw" and "diesel" in f and re.search(r"\b(1|3|5|x1|x3)?\s?(16|18|20|25)d\b|\b(116|118|120|316|318|320|520|525)d\b", f"{c.get('model', '')} {t}".lower()) and c["year"] <= 2014},
    {"key": "dq200", "label": "Boîte DSG 7 (sèche)",
     "tip": "Mécatronique et embrayage fragiles. Fais un essai avec des démarrages lents et des à-coups, demande l'historique.",
     "test": lambda c, t, f: c["make_k"] in VAG and "auto" in c["gear_k"] and ("dsg" in t or re.search(r"\b1[.,][0-4]\b", t)) and c["year"] <= 2016},
    {"key": "powershift", "label": "Boîte Ford Powershift",
     "tip": "Embrayage à double plateau sec qui broute et claque. Fais un essai en ville, demande si l'embrayage a été changé sous garantie.",
     "test": lambda c, t, f: c["make_k"] == "ford" and "auto" in c["gear_k"] and re.search(r"fiesta|focus|b-max|c-max|ecosport", (c.get("model") or "").lower()) and 2011 <= c["year"] <= 2018},
]


def _h5ft(c: dict, t: str) -> bool:
    """1.2 TCe Renault/Dacia (115 à 130 ch) et 1.2 DIG-T Nissan : même moteur H5Ft."""
    if c["make_k"] in ("renault", "dacia"):
        return bool(re.search(r"\b1[.,]2\s*tce|\btce\s*(1(15|18|20|25|30))\b", t))
    if c["make_k"] == "nissan":
        return bool(re.search(r"\b1[.,]2\s*dig-?t|\bdig-?t\s*1(15|30)\b", t))
    return False


def engine_risk(c: dict):
    if not c.get("year"):
        return None
    import unicodedata
    make = unicodedata.normalize("NFKD", c.get("make") or "").encode("ascii", "ignore").decode()   # Citroën -> Citroen
    make_k = scraper.slug(make).replace("-", " ")
    make_k = {"mercedes benz": "mercedes", "ds automobiles": "ds"}.get(make_k, make_k)
    c2 = dict(c, make_k=make_k, gear_k=(c.get("gear") or "").lower())
    text = f"{c.get('version') or ''} {c.get('subtitle') or ''}".lower()
    fuel = (c.get("fuel") or "").lower()
    for r in ENGINE_RISKS:
        try:
            if r["test"](c2, text, fuel):
                return {"key": r["key"], "label": r["label"], "tip": r["tip"]}
        except Exception:
            continue
    return None


def flag_reasons(c: dict) -> list[str]:
    """Raisons de douter d'une annonce, d'après les données de la liste."""
    reasons = []
    if c.get("price") and c["price"] < MIN_PRICE:
        reasons.append(f"prix sous {MIN_PRICE} €")
    if c.get("km") is not None and c.get("year") and c["km"] < 3000 and datetime.now().year - c["year"] >= 3:
        reasons.append(f"kilométrage douteux ({c['km']} km pour une {c['year']}) : sûrement mal saisi")
    title = f"{c.get('version') or ''} {c.get('subtitle') or ''}"
    for m in RED_FLAGS.finditer(title):
        low = title.lower()
        if NEGATION.search(low[max(0, m.start() - 40):m.start()]) or ABSTAIN.search(low[m.end():m.end() + 40]):
            continue
        reasons.append(f"titre : « {m.group(0).strip()} »")
        break
    return reasons


# Phrases qui, dans une description, signalent vraiment un souci (les mots seuls comme « accident » ou
# « problème » sont trop souvent niés : « jamais accidentée », « aucun problème »).
DESC_FLAGS = [
    # --- français
    (r"moteur (hs|mort|cass[ée]|à refaire|a refaire)", "moteur HS"),
    (r"bo[iî]te (de vitesses? )?(hs|morte|cass[ée]e?|à refaire|a refaire)", "boîte HS"),
    (r"joint de culasse", "joint de culasse"),
    (r"ne (d[ée]marre|roule) (plus|pas)", "ne démarre/roule pas"),
    (r"non roulante?", "non roulant"),
    (r"[ée]pave", "épave"),
    (r"pour (les )?pi[eè]ces", "pour pièces"),
    (r"(en|tomb[ée]e? en) panne", "en panne"),
    (r"voyant moteur (allum|on\b)", "voyant moteur allumé"),
    (r"probl[eè]mes? (de |d'|au |à la |a la )?(moteur|bo[iî]te|embrayage|turbo|injecteurs?|distribution|frein|direction|[ée]lectrique)", "problème mécanique"),
    (r"accident[ée]e?s?|sinistr[ée]e?|d[ée]g[aâ]ts? (de )?carross", "accidentée"),
    (r"(uniquement|seulement|r[ée]serv[ée]e?|r[ée]serv[ée]e? aux?|vente|prix|pour) (aux |pour )?(les )?(marchands?|n[ée]gociants?|export|professionnels?)", "vente marchand/export"),
    # --- néerlandais
    (r"motor (kapot|defect|stuk|hs)|motorschade|motorprobleem|problemen? met (de )?motor", "moteur HS (nl)"),
    (r"(versnellingsbak|bak|koppeling|turbo|distributie) (kapot|defect|stuk)|(versnellingsbak|koppelings?)probleem|problemen? met (de )?(versnellingsbak|koppeling|turbo)", "problème mécanique (nl)"),
    (r"koppakking", "joint de culasse (nl)"),
    (r"(start|rijdt|loopt) niet( meer)?|niet (meer )?rijdend|niet rijklaar|rijdt niet", "ne démarre/roule pas (nl)"),
    (r"voor (de )?onderdelen|voor sloop|sloopauto|wrak", "pour pièces (nl)"),
    (r"\bpech\b|\bdefect\b", "en panne (nl)"),
    (r"motorlampje (brandt|aan)|motorlicht (brandt|aan)", "voyant moteur (nl)"),
    (r"ongevalwagen|ongeval gehad|\bschade\b|beschadigd|aanrijding", "accidentée (nl)"),
    (r"(enkel|alleen|uitsluitend|voor) (voor |aan )?(de )?(handelaars?|handelaren|opkopers?|export|garages?)|handelsprijs|\bvoor export\b", "vente marchand/export (nl)"),
    # --- anglais
    (r"engine (problem|failure|damage|issue|knock|is dead|blown)|blown engine|head gasket", "problème moteur (en)"),
    (r"gearbox (problem|issue|failure)|clutch (problem|slipping|gone)", "problème mécanique (en)"),
    (r"(does ?n.?t|won.?t|will not|not) (start|run|drive)|non[- ]runner|not running", "ne démarre/roule pas (en)"),
    (r"for (spare )?parts|spares (or|and) repairs?", "pour pièces (en)"),
    (r"accident damage|damaged|crashed|write[- ]off", "accidentée (en)"),
    (r"(dealers?|traders?|trade|export) only|only for (dealers?|traders?|export)|for export\b", "vente marchand/export (en)"),
]
NEGATION = re.compile(r"\b(aucun|aucune|pas|sans|jamais|non|ni|z[ée]ro|no|geen|nooit|zonder|niet|never|not|without|none)\b[^.!?\n]{0,25}$")
ABSTAIN = re.compile(r"^[-\s]{0,2}(free|vrij)\b|^[^.!?\n]{0,25}(s'abstenir|sabstenir|abstenir|pas int[ée]ress|non merci|niet gewenst|"
                     r"geen interesse|niet ge[iï]nteresseerd|please|no thanks|not interested|bespaar)")


def description_reasons(c: dict, text: str) -> list[str]:
    """Compare la description à la fiche : autre marque, année plus ancienne, vrais signaux de problème."""
    reasons, low = [], text.lower()
    for pat, lab in DESC_FLAGS:
        for m in re.finditer(pat, low):
            before, after = low[max(0, m.start() - 40):m.start()], low[m.end():m.end() + 40]
            if NEGATION.search(before) or ABSTAIN.search(after):
                continue
            reasons.append(f"description : {lab} (« {low[m.start():m.end()].strip()} »)")
            break
    norm = low.replace("ë", "e").replace("é", "e")
    make = (c.get("make") or "").lower().replace("ë", "e")
    model = (c.get("model") or "").lower()
    aliases = {make, *MAKE_ALIASES.get(make, [])} - {""}
    own_named = any(re.search(rf"\b{re.escape(a)}\b", norm) for a in aliases) or (len(model) >= 2 and re.search(rf"\b{re.escape(model)}\b", norm))
    others = [mk for mk in MAKES if re.search(rf"\b{re.escape(mk.replace('ë', 'e'))}\b", norm) and mk.replace("ë", "e") not in aliases]
    if others and make and not own_named:
        reasons.append(f"la description parle d'une {others[0].title()}")
    if c.get("year"):
        # Seules les années bien PLUS ANCIENNES que la fiche comptent (les années récentes sont des dates
        # de contrôle ou d'entretien), et jamais un nombre qui fait partie du nom du modèle (Peugeot 2008…).
        model_nums = set(re.findall(r"\d{4}", f"{c.get('model') or ''} {c.get('version') or ''}"))
        no_prices = re.sub(r"(€|eur\w*)\s*\d[\d .,]*|\d[\d .,]*\s*(€|eur\w*|km\b|cc\b|cm3)", " ", low)   # 1990 €, 2000 km…
        model_nums |= {str(c.get("price") or "")}
        years = {int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-3]\d)\b", no_prices) if y not in model_nums}
        older = sorted(y for y in years if y <= c["year"] - 4)
        if older and c["year"] not in years and (c["year"] - 1) not in years:
            reasons.append(f"fiche : {c['year']}, description : {older[0]}")
    return reasons


DESC_VERSION = 3   # augmenter quand les règles changent : les descriptions gardées sont réévaluées


def check_descriptions(cars: list[dict], previous: dict):
    """Ouvre les annonces qui ressemblent à de bonnes affaires et vérifie leur description.
    Le texte est gardé d'un passage à l'autre : une annonce n'est lue qu'une fois, et ses
    signaux sont recalculés si les règles changent."""
    todo = []
    for c in cars:
        old = previous.get(c["id"]) or {}
        if old.get("desc") is not None:
            c["desc"] = old["desc"]
            c["desc_flags"] = description_reasons(c, c["desc"]) if c["desc"] else []
        elif old.get("desc_v") == DESC_VERSION and "desc_flags" in old and old.get("price") == c.get("price"):
            c["desc_flags"] = old["desc_flags"]
        elif not c.get("suspicious") and c.get("url") and is_deal(c):
            todo.append(c)
        if "desc_flags" in c:
            c["desc_v"] = DESC_VERSION
    todo.sort(key=lambda c: c["delta"] if c.get("delta") is not None else 0)
    checked = 0
    from concurrent.futures import ThreadPoolExecutor
    import threading
    blocked = threading.Event()
    t0 = time.time()

    def read(c):
        if blocked.is_set():
            return c, None, "ignorée"
        try:
            return c, scraper.fetch_description(c["url"]), None
        except Exception as e:
            if "403" in str(e) or "429" in str(e):
                blocked.set()                  # AutoScout24 freine : on arrête de lire pour ce passage
            return c, None, e
    with ThreadPoolExecutor(max_workers=scraper.WORKERS) as ex:
        for c, text, err in ex.map(read, todo[:DESC_CHECKS_PER_RUN]):
            if err is not None:
                if err != "ignorée":
                    print(f"  description illisible ({err})")
                continue
            c["desc"] = (text or "")[:1500]
            c["desc_flags"] = description_reasons(c, text) if text else []
            c["desc_v"] = DESC_VERSION
            checked += 1
    if checked:
        print(f"  descriptions lues en {time.time() - t0:.0f} s")
    for c in cars:
        if c.get("desc_flags"):
            c["flags"] = list(dict.fromkeys((c.get("flags") or []) + c["desc_flags"]))
            c["suspicious"] = True
    if checked:
        flagged = sum(1 for c in todo[:DESC_CHECKS_PER_RUN] if c.get("desc_flags"))
        print(f"  {checked} description(s) lue(s), {flagged} avec un vrai signal de problème")


def add_deal_score(cars: list[dict], reference: list[dict] | None = None):
    reference = reference or []
    for c in cars + reference:
        c["price_label"] = (c.get("price_label") or "").strip() or None
        c["flags"] = flag_reasons(c)
        c["suspicious"] = bool(c["flags"])
    for c in cars:
        c["engine_risk"] = engine_risk(c)
    predict, infos, oof = price_model(cars + reference)
    if infos:
        import math
        errs = sorted(abs(math.log(c["price"] / oof[c["id"]])) for c in cars if c["id"] in oof)
        infos["typical_error"] = round(math.exp(errs[len(errs) // 2]) - 1, 3) if errs else None
        infos["n_search"] = sum(1 for c in cars if c["id"] in oof)
    for c in cars:
        c["expected_price"] = None
        c["delta"] = None
        if predict and c.get("price") and c["price"] >= MIN_PRICE:
            exp = oof.get(c["id"]) or predict(c)
            if exp and exp > 0:
                c["expected_price"] = round(exp, -1)
                c["delta"] = round(c["price"] / exp - 1, 4)
                if c["delta"] <= TOO_GOOD and not c["suspicious"]:
                    c["flags"].append(f"{round(-c['delta'] * 100)} % sous le prix attendu : trop beau pour être vrai")
                    c["suspicious"] = True
    return infos


def is_deal(c: dict) -> bool:
    if c.get("suspicious"):
        return False
    d = c.get("delta")
    if d is not None and d <= DEAL_THRESHOLD:
        return True
    # L'étiquette AS24 (« très bon prix » / « prix très bas ») compte seulement si notre calcul
    # ne la contredit pas : jamais de « bonne affaire » au-dessus du prix moyen.
    return c.get("price_label") in ("top-price", "toolow-price") and (d is None or d <= 0)


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

def scrape_all(params: dict, max_pages: int):
    """Toutes les pages d'une recherche, en parallèle. Renvoie (annonces, total, pages, dernière page lue, erreur)."""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    cars, seen, total, pages, error, last_ok = [], set(), None, None, None, 0
    results = {}
    try:
        results[1] = scraper.scrape_page(params, 1)
    except Exception as e:
        error = f"page 1 : {e}"
    if 1 in results:
        total, pages = results[1]["total"], results[1]["pages"]
        print(f"  {total} résultats annoncés sur {pages} pages (source : {results[1]['source']})")
        last = min(max_pages, pages or 1) if results[1]["listings"] else 1

        def one(pg):
            try:
                return pg, scraper.scrape_page(params, pg), None
            except Exception as e:
                return pg, None, e
        with ThreadPoolExecutor(max_workers=scraper.WORKERS) as ex:
            for pg, res, err in ex.map(one, range(2, last + 1)):
                if err is not None:
                    error = error or f"page {pg} : {err}"
                else:
                    results[pg] = res
    page = 1
    while page in results:   # dans l'ordre, jusqu'au premier trou
        res = results[page]
        fresh = [c for c in res["listings"] if c.get("id") and c["id"] not in seen]
        seen.update(c["id"] for c in fresh)
        cars += fresh
        if not res["listings"]:
            break
        last_ok = page
        page += 1
    if error:
        print("  ERREUR", error)
    print(f"  {len(cars)} annonces sur {last_ok} pages en {time.time() - t0:.0f} s")
    return cars, total, pages, last_ok, error


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
    cars, total, pages, last_ok, error = scrape_all(params, max_pages)

    # Référence de prix : la même recherche sans plafond serré, pour que le « prix moyen »
    # ne soit pas calculé seulement sur les voitures les moins chères.
    reference, ref_info = [], None
    if params.get("price_to") or params.get("price_from"):
        ref_params = {k: v for k, v in params.items() if k not in ("price_from", "price_to", "sort")}
        cap = None
        if params.get("price_to"):
            cap = int(round(float(params["price_to"]) * REF_CAP_FACTOR, -3))
            ref_params["price_to"] = str(cap)
        print(f"  Référence de prix : même recherche{f' jusqu’à {cap} €' if cap else ' sans prix minimum'}")
        own = {c["id"] for c in cars}
        ref_cars, ref_total, _, _, ref_err = scrape_all(ref_params, REF_MAX_PAGES)
        reference = [c for c in ref_cars if c["id"] not in own]
        ref_info = {"n": len(reference), "cap": cap, "total": ref_total, "error": ref_err}

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

    model = add_deal_score(cars, reference)
    if model and ref_info:
        model["reference"] = ref_info
    if cars:
        check_descriptions(cars, previous)

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
                                                  "gear", "power", "zip", "city", "seller_name", "image", "delta", "expected_price", "suspicious", "flags", "engine_risk")}
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
    ap.add_argument("--due-only", action="store_true", help="passage planifié : ne scrape que les recherches pas mises à jour depuis interval_hours")
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

    if a.due_only and not cli:
        hours = float(cfg.get("interval_hours", 6))
        due = []
        for srch in searches:
            f = DATA / f"{search_slug(srch)}.json"
            last = None
            if f.exists():
                try:
                    last = json.loads(f.read_text("utf-8")).get("updated")
                except ValueError:
                    last = None
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
            if age_h is None or age_h >= hours - 0.4:   # marge : un passage un peu en avance compte
                due.append(srch)
                print(f"À mettre à jour : {search_label(srch)} ({'jamais' if age_h is None else f'il y a {age_h:.1f} h'})")
            else:
                print(f"À jour : {search_label(srch)} (il y a {age_h:.1f} h)")
        if not due:
            print("Rien à faire pour ce passage.")
            return
        searches = due

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
