# ARISE — Service Nutrition

Service autonome de génération de programmes alimentaires personnalisés :
profil nutritionnel, catalogue de plats, génération déterministe, chiffrage des
achats.

* Spécification fonctionnelle : [`ARISE_Module_Nutrition_Fonctionnalites.md`](ARISE_Module_Nutrition_Fonctionnalites.md) (v2.0)
* Audit et estimations : [`ARISE_Nutrition_Grille_Analyse.md`](ARISE_Nutrition_Grille_Analyse.md)
* Contrat de saisie du catalogue : [`seeds/README.md`](seeds/README.md)

FastAPI · SQLAlchemy 2.0 · PostgreSQL · Alembic · Python 3.14

---

## État d'avancement

**Lot 1 — Fondations : livré côté service Nutrition.**

Il reste deux points, aucun des deux dans ce dépôt : la migration RS256 de
NestJS, et la production du catalogue avec sa validation nutritionniste.
L'avancement exact est mesuré en continu par `/console`.

| Élément | État |
|---|---|
| Schéma des lots 1 et 2 + migration Alembic | ✅ 28 tables, 20 types énumérés, 44 contraintes `CHECK` |
| Contrat de seed + chargeur idempotent | ✅ `python -m app.seed` |
| Calcul nutritionnel (D-11), propagation des allergènes, tags dérivés | ✅ `app/services/nutrition.py` |
| Conversions d'unités, dont unités locales (FN-012) | ✅ `app/services/units.py` |
| Enveloppe d'erreur et codes stables (FN-037) | ✅ alignée sur NestJS |
| Corrélation `X-Request-ID` (FN-036) | ✅ acceptée en entrée et renvoyée |
| Sondes `/health` et `/ready` | ✅ `/ready` interroge réellement la base |
| **Vérification JWT RS256 via JWKS (§4.3)** | ✅ `app/core/auth.py` — HS256, `aud`, `iss` et expiration refusés |
| **API profil, préférences, allergies, restrictions (FN-001 → FN-004)** | ✅ `app/routers/profile.py` |
| **API catalogue et workflow de validation (FN-007 → FN-009)** | ✅ `app/routers/catalog.py` |
| **Journal d'audit (FN-036)** | ✅ `app/services/audit.py` — masquage des données de santé et des secrets |
| **Interface HTML interne (FN-034)** | ✅ `/console` — saisie, signature, file de validation, avancement mesuré |
| Migration RS256 + JWKS **côté NestJS** | ⬜ à faire — hors de ce dépôt |
| Catalogue seedé (~150 ingrédients, ~180 plats, 100 % signés) | ⬜ production de contenu + nutritionniste |

---

## Démarrage

### Prérequis

PostgreSQL 16 accessible, et une base dédiée :

```sql
CREATE DATABASE arise_nutrition;
```

### Installation

```bash
python -m venv venv
venv/Scripts/activate                  # Windows
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env                   # puis renseigner les valeurs
alembic upgrade head
python -m app.seed
python scripts/generate_dev_keys.py    # jetons de test, développement uniquement
```

> `scripts/generate_dev_keys.py` produit une paire RSA locale dans `keys/`, qui
> permet d'émettre des jetons de test depuis le banc d'essai tant que NestJS n'a
> pas migré en RS256. `keys/*.pem` est ignoré par Git, et **le service refuse de
> démarrer** si ces variables sont renseignées avec `ENVIRONMENT=production` (D-03).

### Lancer le service

```bash
python run.py                          # http://localhost:8000/docs
```

> **Sous Windows**, passer par `run.py` : psycopg 3 refuse la boucle
> `ProactorEventLoop` par défaut, et `run.py` démarre uvicorn sur une
> `SelectorEventLoop`. Sous Linux, `uvicorn app.main:app` fonctionne directement.

### Banc d'essai interne (FN-034)

Service démarré, ouvrir **<http://localhost:8000/console>**.

La page éprouve depuis un navigateur tout ce que le lot 1 livre réellement :

| Section | Ce qu'elle permet de vérifier |
|---|---|
| **Avancement du lot 1** | les dix critères de fin du §15, **mesurés à chaque affichage** : base interrogée, routes introspectées, schémas inspectés, JWKS sondé |
| État du service | `/health`, `/ready`, et la propagation de `X-Request-ID` — chaque appel de la page l'envoie et signale si le service l'a renvoyé à l'identique (FN-036) |
| **Authentification** | émission d'un jeton de développement par rôle, et les quatre refus que la vérification doit produire : HS256, audience incorrecte, émetteur incorrect, jeton expiré (D-03) |
| **Profil utilisateur** | profil, allergies, restrictions et préférences (FN-001 → FN-004), dont le signalement des restrictions non normalisables et l'export §4.6 |
| **Saisie et validation** | création d'ingrédient, signature de la grille allergènes, création de plat, file de validation avec `submit` / `validate` / `publish` / `archive` (FN-007 → FN-009) |
| Conversion d'unités | les trois règles de FN-012, et surtout leurs **refus** : unité locale non mesurée, dénombrement vers masse, quantité nulle |
| Dérivation d'un plat | valeurs par portion, propagation des allergènes, tags et restrictions dérivés, verdict de publication (D-11, FN-003, FN-008, FN-009) |
| Catalogue versionné | `--dry-run` et application du seed, avec le détail des publications refusées (FN-040) |
| Catalogue en base | ingrédients et plats, chaque plat **confronté à son recalcul** — un écart signale une valeur saisie à la main |
| Enveloppe d'erreur | un bouton par code de FN-037, plus une exception non prévue pour vérifier qu'aucun détail technique ne fuit |
| Fournisseur IA | joignabilité d'OpenRouter |

Le catalogue de démarrage n'étant pas signé, la console affiche des refus de
publication et un compteur d'ingrédients non vérifiés non nul. C'est le
comportement attendu de FN-003, pas un défaut de configuration.

> La console est **refusée en production** (`ENVIRONMENT=production` → 404) :
> elle n'est pas authentifiée, expose l'état interne du service et sait écrire
> en base.

Ce n'est pas encore l'interface de saisie décrite par FN-034 : les API
d'écriture du catalogue et la file de validation restent à écrire.

### Tests

```bash
python -m pytest
```

Les tests de calcul et de contrat n'ont besoin d'aucune base : ils tournent en
une seconde. C'est délibéré — les règles de sécurité alimentaire doivent être
vérifiables à chaque commit, sans excuse pour être désactivées.

---

## Organisation du code

```
app/
  core/          configuration, base de données, journalisation, erreurs, auth
  models/        schéma SQLAlchemy (lots 1 et 2)
  schemas/       contrats HTTP d'entrée et de sortie
  services/      règles métier pures, testables sans base
  routers/       points d'entrée HTTP
  templates/     banc d'essai interne (FN-034)
  seed/          contrat YAML et chargeur idempotent
migrations/      Alembic
scripts/         outillage hors serveur (clés de développement)
seeds/           catalogue versionné (ingrédients, plats)
tests/           tests unitaires
```

### Ce que la suite de tests protège

175 tests, aucun n'a besoin de PostgreSQL. Les quatre modules ajoutés au lot 1
couvrent ce qui, en cas de régression, deviendrait un incident plutôt qu'un bug :

| Fichier | Ce qu'il tient |
|---|---|
| `test_auth.py` | les cinq refus de jeton, et le refus de démarrer en production avec une clé de signature |
| `test_isolation.py` | **aucune route de profil ne peut désigner un autre utilisateur** — vérifié par introspection, donc une route ajoutée demain sans garde fera échouer le test |
| `test_catalog_rules.py` | ce qui ne se saisit pas (valeurs dérivées, statut, tags calculés), la table des transitions, l'ordre des refus de publication |
| `test_audit_et_restrictions.py` | le masquage des données de santé et des secrets, et le refus d'appliquer une restriction à moitié normalisée |

### Deux principes structurants

**Le modèle IA ne décide jamais.** Sélection, filtrage, calculs et validation
relèvent exclusivement du code Python et de SQL. Le LLM rédige la justification
d'un programme déjà entièrement décidé, en **un seul appel par programme** — une
contrainte `CHECK` sur `recommendation_runs.llm_call_count` rend la dérive
impossible, pas seulement improbable.

**Les valeurs dérivées ne se saisissent pas.** Valeurs nutritionnelles d'un plat,
allergènes, tags de régime, restrictions compatibles : tout est calculé depuis
les ingrédients et recalculé à chaque modification. Le chargeur de seed refuse
les fichiers qui tentent de les saisir à la main.

---

## Sécurité

* **Aucun secret dans le dépôt.** `.env` est ignoré par Git ; partir de
  `.env.example`.
* La clé publique JWT est récupérée via **JWKS** ; le service ne détient jamais
  la clé privée de signature ARISE (D-03).
* Aucune donnée d'identité ARISE n'est persistée : seul l'`external_user_id`
  opaque issu du claim `sub` (D-04).
* Aucune donnée de santé en clair dans les journaux applicatifs (FN-036).

### Ce que la vérification de jeton refuse

Ces refus sont couverts par `tests/test_auth.py`, et rejouables depuis la
section « Authentification » du banc d'essai :

| Cas | Motif |
|---|---|
| Signature HS256 | Un secret symétrique partagé permettrait à ce service de forger des jetons administrateur ARISE (piège n° 4) |
| `aud` ne désignant pas le service | Un jeton émis pour un autre service ne vaut pas ici |
| `iss` inattendu | L'émetteur doit être l'API ARISE |
| Jeton expiré | — |
| `sub` absent | Sans lui, le profil ne serait rattaché à personne (D-04) |

Le message renvoyé au client reste « Authentification requise. » dans tous les
cas : distinguer « signature invalide » d'« audience incorrecte » aide surtout
un attaquant. Le motif précis part au journal.

### Dette à traiter

| Gravité | Point |
|---|---|
| 🔴 | La clé OpenRouter présente dans `.env` a circulé en clair : **à révoquer et régénérer**. |
| 🟠 | Côté `arise_BE`, `.env` est suivi par Git avec des secrets réels — rotation nécessaire. |
| 🟠 | Côté `arise_BE`, `synchronize: true` cohabite avec 17 migrations TypeORM. |
