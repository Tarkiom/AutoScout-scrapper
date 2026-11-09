# AutoScout Scraper

Scrape les annonces AutoScout24 avec GitHub Actions et publie les résultats sur une page GitHub Pages : stats, nuage prix/km, bonnes affaires, nouvelles annonces et baisses de prix.

## Mise en route (une seule fois)
1. **Settings → Pages** : Source = *Deploy from a branch*, branche `main`, dossier `/docs`, puis **Save**.
   La page sera sur `https://tarkiom.github.io/AutoScout-scrapper/`
2. **Settings → Actions → General → Workflow permissions** : coche *Read and write permissions*, puis **Save**.
3. *(Optionnel)* Alertes : **Settings → Secrets and variables → Actions → New repository secret**
   - `NTFY_TOPIC` : nom de canal [ntfy](https://ntfy.sh) (app gratuite) pour recevoir les notifications sur ton téléphone ;
   - `DISCORD_WEBHOOK` : URL du webhook d'un salon Discord.
   Tu reçois les nouvelles bonnes affaires et les baisses de prix des recherches surveillées.

## Lancer un scraping
- **Depuis le site** : bouton **Nouvelle recherche**. La première fois, le site demande une clé GitHub :
  token *fine-grained* limité à ce repo, permission **Actions : Read and write** uniquement. Elle reste dans le navigateur de ton appareil.
  Le site lance le workflow, suit sa progression et affiche les résultats dès qu'ils sont publiés.
  L'option *Surveiller toutes les 6 h* ajoute la recherche à `config.json`.
- **À la main** : onglet **Actions** → *Scraper AutoScout24* → **Run workflow**.
  Remplis marque et modèle (slugs AutoScout24 : `volkswagen`, `mercedes-benz`, `classe-a`…) et les filtres voulus.
  Laisse marque et modèle vides pour lancer les recherches de `config.json`.
- **Automatique** : toutes les 6 h sur les recherches de `config.json`. Pour en ajouter une, édite ce fichier :
  ```json
  { "name": "Golf diesel", "make": "volkswagen", "model": "golf", "fuel": "D", "price_to": 15000, "km_to": 150000, "zip": "6110", "radius": 50 }
  ```
  Filtres possibles : `country` (B, D, F, NL, L…), `price_from`, `price_to`, `year_from`, `year_to`, `km_from`, `km_to`,
  `fuel` (B essence, D diesel, E électrique, 2 hybride, L GPL, C CNG), `gear` (M, A), `zip` + `radius`,
  `sort` (standard, prix_asc, prix_desc, km_asc, annee_desc, recent).

Chaque run :
- parcourt toutes les pages (20 annonces par page, `max_pages` dans `config.json`) ;
- marque les annonces **nouvelles** depuis le passage précédent et les **baisses de prix** ;
- calcule l'écart de chaque annonce par rapport à la tendance prix/km et reprend l'évaluation d'AutoScout24 (« bon prix »…) ;
- envoie les nouvelles bonnes affaires sur Discord (si le secret est configuré) ;
- commit le résultat dans `docs/data/`, et la page se met à jour en 1 à 2 min.

## Sur le site
- **Prix attendu** pour chaque annonce, calculé selon l'âge, les km, la puissance, le carburant, la boîte et le type de vendeur. Le site affiche la précision du modèle et l'effet de chaque critère.
- **Bonne affaire** : au moins 15 % sous le prix attendu, ou « très bon prix » selon AutoScout24. Les annonces suspectes (épave, export, prix anormal) sont écartées.
- **Négo** : prix à viser, selon la durée en ligne, les baisses déjà faites et l'écart au prix attendu.
- **Historique de prix** de chaque annonce, **durée en ligne** et annonces **vendues ou retirées** (avec leur délai de vente).
- **Le marché** : prix médian et fourchette, jour après jour.
- **Distance** depuis ton code postal (réglages), tri par proximité et rayon max.
- **Favoris** (★, gardés sur l'appareil) et **comparateur** jusqu'à 3 voitures.
- **Moteurs à surveiller** 🔴 / 🟠 / 🟡 d'après l'[Encyclopédie des moteurs à éviter en occasion](https://claude.ai/artifact/62bhyuTsn4U2Rhtd31qNHQ) (17 moteurs, périodes et conseils d'achat), plus deux boîtes auto fragiles.
- **App installable** : dans Safari, Partager → *Sur l'écran d'accueil*.

## En local
```
pip install -r requirements.txt
python scrape.py --make volkswagen --model polo --price-to 15000
```
Ouvre ensuite `docs/index.html` via un petit serveur (`python -m http.server -d docs`).

## Si ça bloque
- **Erreur 403 dans les logs** : AutoScout24 bloque les IP de GitHub. Augmente `delay_seconds`, réduis `max_pages` ou passe par un proxy.
- **0 annonce** : vérifie les slugs marque/modèle en les copiant depuis l'URL d'AutoScout24.

Usage perso et à volume raisonnable : les CGU d'AutoScout24 interdisent le scraping automatisé.
