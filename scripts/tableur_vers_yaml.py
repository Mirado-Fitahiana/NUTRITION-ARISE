"""Gabarit tableur → YAML de seed — sprint 03, §4.

    python scripts/tableur_vers_yaml.py --gabarit saisie/     # crée les modèles CSV
    python scripts/tableur_vers_yaml.py --depuis saisie/ --vers seeds/ --dry-run
    python scripts/tableur_vers_yaml.py --depuis saisie/ --vers seeds/

Le catalogue vit en YAML versionné, et c'est un bon choix : il passe en revue de
code et l'historique Git porte la trace de responsabilité. Mais **le
contributeur n'a pas à écrire du YAML à la main** — un nutritionniste travaille
au tableur. Ce script fait le pont.

Il ne réimplémente pas le contrat de saisie : il valide chaque ligne avec
`SeedIngredient` et `SeedDish`, les modèles mêmes qu'utilise le chargeur. Une
colonne mal remplie échoue donc ici avec le message que le chargeur aurait
produit, avant d'atteindre la revue.

Les colonnes multivaluées se remplissent avec des `;` (`peanut;milk`). Les
étapes de recette utilisent `|`, puisqu'une étape peut contenir un `;`.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.seed.schemas import SeedDish, SeedIngredient  # noqa: E402

FICHIER_INGREDIENTS = "ingredients.csv"
FICHIER_CONVERSIONS = "conversions.csv"
FICHIER_PLATS = "plats.csv"
FICHIER_PLATS_INGREDIENTS = "plats_ingredients.csv"

COLONNES_INGREDIENTS = [
    "slug", "name", "category", "reference_unit",
    "nutrition_source", "kcal_100", "protein_100", "carbs_100", "fat_100", "fiber_100",
    "allergenes", "allergenes_source", "allergenes_verified_by", "allergenes_verified_at",
    "restriction_flags", "locally_available", "seasonality", "density_g_per_ml",
    "aliases",
]
COLONNES_CONVERSIONS = [
    "ingredient_slug", "from_unit", "to_unit", "factor", "source", "measured_at",
]
COLONNES_PLATS = [
    "slug", "name", "description", "meal_types", "origin",
    "prep_time_min", "cook_time_min", "difficulty", "servings",
    "estimated_cost", "cost_class", "currency",
    "etapes", "tags", "compatible_goals",
    "author", "validated_by", "validated_at", "publish",
]
COLONNES_PLATS_INGREDIENTS = [
    "plat_slug", "ingredient", "quantity", "unit", "note",
]

EXEMPLE_INGREDIENT = {
    "slug": "riz-blanc-cru", "name": "Riz blanc (cru)",
    "category": "cereals", "reference_unit": "g",
    "nutrition_source": "CIQUAL 2020 — Riz blanc cru",
    "kcal_100": "349", "protein_100": "7.1", "carbs_100": "77.9",
    "fat_100": "0.9", "fiber_100": "1.4",
    "allergenes": "", "allergenes_source": "", "allergenes_verified_by": "",
    "allergenes_verified_at": "",
    "restriction_flags": "vegetarian;vegan", "locally_available": "oui",
    "seasonality": "", "density_g_per_ml": "0.85", "aliases": "Vary:mg",
}
EXEMPLE_CONVERSION = {
    "ingredient_slug": "riz-blanc-cru", "from_unit": "kapoaka", "to_unit": "g",
    "factor": "285", "source": "measured", "measured_at": "2026-09-10",
}
EXEMPLE_PLAT = {
    "slug": "vary-amin-anana", "name": "Vary amin'anana",
    "description": "Riz aux brèdes, plat quotidien malgache.",
    "meal_types": "lunch;dinner", "origin": "Madagascar",
    "prep_time_min": "15", "cook_time_min": "30", "difficulty": "easy", "servings": "4",
    "estimated_cost": "6000", "cost_class": "economical", "currency": "MGA",
    "etapes": "Rincer le riz jusqu'à ce que l'eau soit claire.|Porter à ébullition avec les brèdes.",
    "tags": "local;family_meal", "compatible_goals": "weight_maintenance;balanced_diet",
    "author": "admin:nom-prenom", "validated_by": "", "validated_at": "", "publish": "non",
}
EXEMPLE_PLAT_INGREDIENT = {
    "plat_slug": "vary-amin-anana", "ingredient": "riz-blanc-cru",
    "quantity": "300", "unit": "g", "note": "",
}


class ErreurSaisie(Exception):
    """Une ligne de tableur refusée, localisée pour le contributeur."""


def _liste(valeur: str | None, separateur: str = ";") -> list[str]:
    if not valeur:
        return []
    return [part.strip() for part in valeur.split(separateur) if part.strip()]


def _booleen(valeur: str | None, *, defaut: bool = False) -> bool:
    if valeur is None or not valeur.strip():
        return defaut
    return valeur.strip().lower() in {"oui", "o", "true", "vrai", "1", "x"}


def _nombre(valeur: str | None) -> Any:
    """Garde la forme saisie : 349 reste un entier, 7.1 un flottant."""
    if valeur is None or not valeur.strip():
        return None
    texte = valeur.strip().replace(",", ".")
    nombre = float(texte)
    return int(nombre) if nombre.is_integer() and "." not in texte else nombre


def _date(valeur: str | None) -> date | None:
    if not valeur or not valeur.strip():
        return None
    return date.fromisoformat(valeur.strip())


def _sans_vide(donnees: dict[str, Any]) -> dict[str, Any]:
    """Retire les clés vides pour que le YAML produit reste lisible."""
    return {
        cle: valeur
        for cle, valeur in donnees.items()
        if valeur not in (None, "", [], {})
    }


def _lire_csv(chemin: Path) -> list[dict[str, str]]:
    if not chemin.exists():
        return []
    with chemin.open(encoding="utf-8-sig", newline="") as flux:
        return [
            {(cle or "").strip(): (valeur or "").strip() for cle, valeur in ligne.items()}
            for ligne in csv.DictReader(flux)
        ]


def construire_ingredient(
    ligne: dict[str, str], conversions: list[dict[str, str]]
) -> dict[str, Any]:
    allergenes = _sans_vide(
        {
            "values": _liste(ligne.get("allergenes")),
            "source": ligne.get("allergenes_source"),
            "verified_by": ligne.get("allergenes_verified_by"),
            "verified_at": _date(ligne.get("allergenes_verified_at")),
        }
    )
    # `values: []` est une affirmation engageante et non une absence : la clé
    # doit rester présente même vide (seeds/README.md).
    allergenes.setdefault("values", [])

    aliases = []
    for brut in _liste(ligne.get("aliases")):
        alias, _, langue = brut.partition(":")
        aliases.append(_sans_vide({"alias": alias.strip(), "language": (langue or "fr").strip()}))

    return _sans_vide(
        {
            "slug": ligne.get("slug"),
            "name": ligne.get("name"),
            "category": ligne.get("category"),
            "reference_unit": ligne.get("reference_unit"),
            "nutrition": _sans_vide(
                {
                    "source": ligne.get("nutrition_source"),
                    "kcal_100": _nombre(ligne.get("kcal_100")),
                    "protein_100": _nombre(ligne.get("protein_100")),
                    "carbs_100": _nombre(ligne.get("carbs_100")),
                    "fat_100": _nombre(ligne.get("fat_100")),
                    "fiber_100": _nombre(ligne.get("fiber_100")),
                }
            ),
            "allergens": allergenes,
            "restriction_flags": _liste(ligne.get("restriction_flags")),
            "locally_available": _booleen(ligne.get("locally_available"), defaut=True),
            "seasonality": [int(m) for m in _liste(ligne.get("seasonality"))],
            "density_g_per_ml": _nombre(ligne.get("density_g_per_ml")),
            "aliases": aliases,
            "unit_conversions": [
                _sans_vide(
                    {
                        "from_unit": c.get("from_unit"),
                        "to_unit": c.get("to_unit"),
                        "factor": _nombre(c.get("factor")),
                        "source": c.get("source"),
                        "measured_at": _date(c.get("measured_at")),
                    }
                )
                for c in conversions
            ],
        }
    )


def construire_plat(ligne: dict[str, str], composants: list[dict[str, str]]) -> dict[str, Any]:
    validation = _sans_vide(
        {
            "validated_by": ligne.get("validated_by"),
            "validated_at": _date(ligne.get("validated_at")),
        }
    )
    if _booleen(ligne.get("publish")):
        validation["publish"] = True

    return _sans_vide(
        {
            "slug": ligne.get("slug"),
            "name": ligne.get("name"),
            "description": ligne.get("description"),
            "meal_types": _liste(ligne.get("meal_types")),
            "origin": ligne.get("origin"),
            "prep_time_min": _nombre(ligne.get("prep_time_min")),
            "cook_time_min": _nombre(ligne.get("cook_time_min")),
            "difficulty": ligne.get("difficulty"),
            "servings": _nombre(ligne.get("servings")),
            "estimated_cost": _nombre(ligne.get("estimated_cost")),
            "cost_class": ligne.get("cost_class"),
            "currency": ligne.get("currency"),
            "ingredients": [
                _sans_vide(
                    {
                        "ingredient": c.get("ingredient"),
                        "quantity": _nombre(c.get("quantity")),
                        "unit": c.get("unit"),
                        "note": c.get("note"),
                    }
                )
                for c in composants
            ],
            "steps": _liste(ligne.get("etapes"), separateur="|"),
            "tags": _liste(ligne.get("tags")),
            "compatible_goals": _liste(ligne.get("compatible_goals")),
            "author": ligne.get("author"),
            "validation": validation,
        }
    )


def _valider(entrees: list[dict[str, Any]], modele: type, etiquette: str) -> list[str]:
    erreurs: list[str] = []
    for index, entree in enumerate(entrees, start=2):  # ligne 1 = en-têtes
        try:
            modele.model_validate(entree)
        except ValidationError as exc:
            slug = entree.get("slug", "?")
            for detail in exc.errors():
                champ = ".".join(str(p) for p in detail["loc"]) or "(ligne)"
                erreurs.append(
                    f"  {etiquette} ligne {index} [{slug}] · {champ} : {detail['msg']}"
                )
    return erreurs


def ecrire_gabarit(dossier: Path) -> None:
    dossier.mkdir(parents=True, exist_ok=True)
    modeles = [
        (FICHIER_INGREDIENTS, COLONNES_INGREDIENTS, EXEMPLE_INGREDIENT),
        (FICHIER_CONVERSIONS, COLONNES_CONVERSIONS, EXEMPLE_CONVERSION),
        (FICHIER_PLATS, COLONNES_PLATS, EXEMPLE_PLAT),
        (FICHIER_PLATS_INGREDIENTS, COLONNES_PLATS_INGREDIENTS, EXEMPLE_PLAT_INGREDIENT),
    ]
    for nom, colonnes, exemple in modeles:
        chemin = dossier / nom
        with chemin.open("w", encoding="utf-8-sig", newline="") as flux:
            writer = csv.DictWriter(flux, fieldnames=colonnes)
            writer.writeheader()
            writer.writerow(exemple)
        print(f"  ✓ {chemin}")


def _dump_yaml(entrees: list[dict[str, Any]], chemin: Path, entete: str) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    corps = yaml.safe_dump(
        entrees,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    chemin.write_text(f"# {entete}\n# Généré par scripts/tableur_vers_yaml.py — relire avant commit.\n{corps}", encoding="utf-8")


def convertir(depuis: Path, vers: Path, *, dry_run: bool) -> int:
    lignes_ingredients = _lire_csv(depuis / FICHIER_INGREDIENTS)
    lignes_conversions = _lire_csv(depuis / FICHIER_CONVERSIONS)
    lignes_plats = _lire_csv(depuis / FICHIER_PLATS)
    lignes_composants = _lire_csv(depuis / FICHIER_PLATS_INGREDIENTS)

    if not lignes_ingredients and not lignes_plats:
        print(f"✗ aucun contenu dans {depuis}", file=sys.stderr)
        return 2

    conversions_par_slug: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ligne in lignes_conversions:
        conversions_par_slug[ligne.get("ingredient_slug", "")].append(ligne)

    composants_par_plat: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ligne in lignes_composants:
        composants_par_plat[ligne.get("plat_slug", "")].append(ligne)

    ingredients = [
        construire_ingredient(ligne, conversions_par_slug.get(ligne.get("slug", ""), []))
        for ligne in lignes_ingredients
    ]
    plats = [
        construire_plat(ligne, composants_par_plat.get(ligne.get("slug", ""), []))
        for ligne in lignes_plats
    ]

    erreurs = _valider(ingredients, SeedIngredient, FICHIER_INGREDIENTS)
    erreurs += _valider(plats, SeedDish, FICHIER_PLATS)

    orphelins = sorted(set(composants_par_plat) - {p.get("slug") for p in plats})
    if orphelins:
        erreurs.append(
            f"  {FICHIER_PLATS_INGREDIENTS} · ingrédients rattachés à un plat absent de "
            f"{FICHIER_PLATS} : {', '.join(orphelins)}"
        )

    print(f"Lu : {len(ingredients)} ingrédient(s), {len(plats)} plat(s)")
    sys.stdout.flush()  # sinon le rapport d'erreurs (stderr) s'affiche avant ce compte

    if erreurs:
        print(f"\n✗ {len(erreurs)} refus de saisie :\n", file=sys.stderr)
        for erreur in erreurs:
            print(erreur, file=sys.stderr)
        print("\nAucun fichier écrit.", file=sys.stderr)
        return 1

    if dry_run:
        print("\n✓ saisie valide — aucun fichier écrit (--dry-run)")
        return 0

    if ingredients:
        cible = vers / "ingredients" / "import-tableur.yaml"
        _dump_yaml(ingredients, cible, "Ingrédients importés depuis le tableur de saisie.")
        print(f"  ✓ {cible}")
    if plats:
        cible = vers / "dishes" / "import-tableur.yaml"
        _dump_yaml(plats, cible, "Plats importés depuis le tableur de saisie.")
        print(f"  ✓ {cible}")

    print("\n✓ conversion terminée — lancer « python -m app.seed --dry-run » avant de commiter")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="python scripts/tableur_vers_yaml.py",
        description="Convertit la saisie tableur du catalogue en YAML de seed.",
    )
    parser.add_argument("--gabarit", type=Path, help="écrit les modèles CSV dans ce dossier")
    parser.add_argument("--depuis", type=Path, help="dossier contenant les CSV remplis")
    parser.add_argument("--vers", type=Path, default=Path("seeds"), help="dossier de seed cible")
    parser.add_argument("--dry-run", action="store_true", help="valide sans écrire")
    args = parser.parse_args()

    if args.gabarit:
        print(f"Gabarits de saisie — {args.gabarit}")
        ecrire_gabarit(args.gabarit)
        print("\nRemplir ces fichiers au tableur, puis :")
        print(f"  python scripts/tableur_vers_yaml.py --depuis {args.gabarit} --vers seeds/ --dry-run")
        return 0

    if not args.depuis:
        parser.error("indiquer --gabarit ou --depuis")

    if not args.depuis.exists():
        print(f"✗ dossier introuvable : {args.depuis}", file=sys.stderr)
        return 2

    return convertir(args.depuis, args.vers, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
