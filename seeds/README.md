# Contrat des fichiers de seed — catalogue ARISE Nutrition

> Référence : FN-040 et D-12 de `ARISE_Module_Nutrition_Fonctionnalites.md` v2.0.
> Ce document est le **contrat de saisie**. Il s'adresse autant au nutritionniste
> validateur qu'aux développeurs.

Le catalogue ne se saisit pas dans une interface web. Il vit **dans le dépôt**,
en YAML versionné, et un script idempotent l'applique. Trois raisons :

1. chaque modification passe en **revue de code** — c'est la relecture qui donne
   sa valeur à la validation nutritionniste ;
2. le catalogue est **rejouable à l'identique** en développement, en recette et
   en production ;
3. l'historique Git est la **trace de responsabilité** : qui a saisi quoi, quand,
   et qui l'a validé.

---

## Organisation

```
seeds/
  ingredients/     un fichier YAML par catégorie
  dishes/          un fichier YAML par type de repas
```

Chaque fichier contient **une liste** d'entrées. Le découpage en fichiers est
libre : seul le `slug` compte, et il doit être unique sur l'ensemble du dossier.

---

## Commandes

```bash
python -m app.seed --dry-run     # valide sans rien écrire — à lancer avant chaque commit
python -m app.seed               # applique (idempotent, rejouable)
```

`--dry-run` valide le format, résout les références croisées, calcule les
valeurs nutritionnelles et **annonce les publications qui seraient refusées**.
C'est la commande à exécuter en intégration continue.

Le seed est idempotent : le rejouer deux fois produit exactement le même état.
Les collections filles (alias, conversions, ingrédients d'un plat, étapes, tags)
sont **remplacées**, ce qui fait que supprimer une ligne du YAML la supprime bien
en base.

---

## Ingrédient

```yaml
- slug: riz-blanc-cru              # minuscules, tirets ; clé fonctionnelle, ne change jamais
  name: Riz blanc (cru)
  category: cereals                # cf. liste des catégories
  reference_unit: g                # g | ml | unit

  nutrition:
    source: "CIQUAL 2020 — Riz blanc cru"   # OBLIGATOIRE, jamais inventé
    kcal_100: 349                  # valeurs POUR 100 UNITÉS DE RÉFÉRENCE
    protein_100: 7.1
    carbs_100: 77.9
    fat_100: 0.9
    fiber_100: 1.4

  allergens:
    values: []                     # cf. liste des 14 allergènes
    # Les trois champs ci-dessous vont ENSEMBLE, ou aucun.
    # Sans eux, l'ingrédient est « non vérifié » et bloque toute publication.
    source: "CIQUAL 2020 + revue nutritionniste"
    verified_by: "nutritionist:nom-prenom"
    verified_at: 2026-09-15

  restriction_flags: [vegetarian, vegan]
  locally_available: true
  seasonality: []                  # mois 1–12 ; vide = toute l'année
  density_g_per_ml: 0.85           # requis pour toute conversion poids ↔ volume

  aliases:
    - { alias: Vary, language: mg }

  unit_conversions:                # conversions MESURÉES, propres à l'ingrédient
    - { from_unit: kapoaka, to_unit: g, factor: 285, source: measured, measured_at: 2026-09-10 }
```

### `reference_unit` — préférer `g` ou `ml`

Les valeurs nutritionnelles s'entendent **pour 100 unités de référence**. Pour un
ingrédient qui se compte (œuf, gousse d'ail), ne pas utiliser `reference_unit: unit` :
choisir `g` et déclarer une conversion `unit → g`. C'est exactement l'objet de
`unit_conversions`, et cela évite des valeurs « pour 100 œufs » illisibles.

### `restriction_flags` — attention à la sémantique

Elle n'est pas uniforme, et c'est la source d'erreur la plus fréquente :

| Drapeau | Signification |
|---|---|
| `vegetarian`, `vegan` | l'ingrédient **convient** à ce régime |
| `pork`, `alcohol`, `lactose`, `gluten` | l'ingrédient **en contient** |

Un plat est végétarien si *tous* ses ingrédients le sont ; il est sans lactose si
*aucun* n'en contient. Oublier `vegetarian` sur un légume rend végétarien aucun
des plats qui l'utilisent.

### 🔴 Le bloc `allergens`

`values: []` signifie « **aucun allergène** ». C'est une affirmation engageante,
pas une donnée manquante — et elle ne vaut que signée.

Tant que `source`, `verified_by` et `verified_at` ne sont pas renseignés,
l'ingrédient est **non vérifié**, et *tout plat qui l'utilise devient non
publiable*. Ce blocage est voulu. Au démarrage du lot 1, il concernera la quasi-
totalité du catalogue : c'est le signal que la validation nutritionniste n'est
pas terminée, et non un défaut à contourner.

---

## Plat

```yaml
- slug: vary-amin-anana
  name: Vary amin'anana
  description: Riz aux brèdes, plat quotidien malgache.
  meal_types: [lunch, dinner]      # breakfast | lunch | dinner | snack
  origin: Madagascar
  prep_time_min: 15
  cook_time_min: 30
  difficulty: easy                 # easy | medium | hard
  servings: 4                      # les quantités ci-dessous valent POUR 4 PORTIONS

  estimated_cost: 6000             # coût indicatif de la recette entière
  cost_class: economical           # economical | medium | high
  currency: MGA

  ingredients:
    - { ingredient: riz-blanc-cru, quantity: 300, unit: g }
    - { ingredient: brede-mafana, quantity: 250, unit: g }

  steps:
    - Rincer le riz jusqu'à ce que l'eau soit claire.
    - Porter à ébullition avec les brèdes.

  tags: [local, family_meal]       # tags SUBJECTIFS uniquement
  compatible_goals: [weight_maintenance, balanced_diet]

  author: "admin:nom-prenom"
  validation:
    validated_by: "nutritionist:autre-personne"   # doit différer de l'auteur
    validated_at: 2026-09-20
    publish: true
```

### Ce qui ne se saisit **jamais**

| Donnée | Origine |
|---|---|
| Valeurs nutritionnelles du plat | Calculées depuis les ingrédients (D-11) |
| Allergènes du plat | Propagés par union depuis les ingrédients |
| Tags `vegetarian`, `vegan`, `lactose_free`, `gluten_free`, `high_protein`, `low_carb` | Déduits — le chargeur **refuse** le fichier s'ils sont saisis |
| Restrictions compatibles | Déduites |

Seuls les tags subjectifs se saisissent : `quick`, `local`, `family_meal`,
`make_ahead`, `seasonal`, `economical`, `muscle_gain`, `weight_loss`.

### Conditions de publication

`publish: true` n'est honoré que si **toutes** ces conditions sont réunies :

- [ ] `validated_by` et `validated_at` renseignés ;
- [ ] `validated_by` différent de `author` ;
- [ ] **tous** les ingrédients du plat ont leurs allergènes vérifiés ;
- [ ] toutes les unités employées sont convertibles vers l'unité de référence de
      leur ingrédient.

Sinon le plat est chargé en `pending_validation` et le motif est affiché dans le
rapport. Le seed n'échoue pas pour autant : il ne publie simplement pas.

---

## Valeurs de référence

### Sources nutritionnelles, par ordre de pertinence

1. **Table de composition des aliments d'Afrique de l'Ouest (FAO / INFOODS)** —
   la plus proche des ingrédients locaux ;
2. **CIQUAL (ANSES)** — francophone, très complète sur les produits transformés ;
3. **USDA FoodData Central** — complément.

Reporter la référence exacte de l'entrée consultée dans `nutrition.source`. « Estimé »
n'est pas une source.

### Catégories d'ingrédient

`cereals` · `vegetables` · `fruits` · `meats` · `fish` · `dairy` · `legumes` ·
`beverages` · `oils` · `spices` · `processed`

### Les 14 allergènes

`peanut` · `tree_nuts` · `milk` · `egg` · `fish` · `crustaceans` · `molluscs` ·
`soy` · `gluten` · `sesame` · `mustard` · `celery` · `sulphites` · `lupin`

### Objectifs

`weight_loss` · `weight_maintenance` · `weight_gain` · `muscle_gain` ·
`balanced_diet` · `habit_improvement`

---

## Volumétrie cible du lot 1

| Élément | Cible |
|---|---|
| Ingrédients | ~150 |
| Plats publiés | ~180 — soit ≈ 30 petits-déjeuners, 75 déjeuners, 75 dîners |
| Ingrédients avec allergènes vérifiés | **100 %** |

Ordre d'exécution : ingrédients → **revue et validation des allergènes par le
nutritionniste** → plats → validation des plats → publication.

> **Signal d'alerte** : si à mi-parcours du lot 1 moins de 60 plats sont saisis,
> le lot 2 glissera mécaniquement. Le seed n'est pas une tâche de finition, c'est
> le chemin critique du projet.

Les périodes de 14 et 21 jours se débloquent automatiquement quand le catalogue
atteint 330 puis 450 plats publiés : c'est le paramètre `max_plan_days`, pas un
développement.
