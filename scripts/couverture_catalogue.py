"""Suivi d'avancement du catalogue — sprint 03, §5.

    python scripts/couverture_catalogue.py            # rapport lisible
    python scripts/couverture_catalogue.py --creneaux # + le détail des trous
    python scripts/couverture_catalogue.py --json     # pour un tableau de bord

Le sprint 03 est éditorial et court sur plusieurs mois : sans un chiffre
rafraîchi, on ne sait pas s'il avance. Ce script donne ce chiffre, et surtout il
signale les combinaisons objectif × restriction × repas **à zéro plat** — celles
pour lesquelles un utilisateur réel n'obtiendrait aucun résultat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.database import create_sync_session_factory  # noqa: E402
from app.models.catalog import Dish, Ingredient  # noqa: E402
from app.models.enums import DishStatus  # noqa: E402
from app.services.couverture import (  # noqa: E402
    CIBLE_INGREDIENTS,
    CIBLE_PAR_TYPE_REPAS,
    CIBLE_PLATS_PUBLIES,
    POOL_MINIMAL_PAR_CRENEAU,
    IngredientResume,
    PlatResume,
    RapportCouverture,
    analyser,
)


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _barre(actuel: int, cible: int, largeur: int = 28) -> str:
    if cible <= 0:
        return ""
    rempli = min(largeur, round(largeur * actuel / cible))
    return "█" * rempli + "░" * (largeur - rempli)


def _lire_catalogue() -> tuple[list[PlatResume], list[IngredientResume]]:
    session_factory = create_sync_session_factory()
    with session_factory() as session:
        plats = [
            PlatResume(
                slug=d.slug,
                status=DishStatus(d.status),
                meal_types=tuple(d.meal_types or ()),
                compatible_goals=tuple(d.compatible_goals or ()),
                compatible_restrictions=tuple(d.compatible_restrictions or ()),
            )
            for d in session.scalars(select(Dish)).all()
        ]
        ingredients = [
            IngredientResume(
                slug=i.slug,
                allergenes_verifies=i.allergen_verified_at is not None
                and i.allergen_verified_by is not None,
            )
            for i in session.scalars(select(Ingredient)).all()
        ]
    return plats, ingredients


def _rendre(rapport: RapportCouverture, detail_creneaux: bool) -> str:
    lignes: list[str] = []
    a = lignes.append

    a("Couverture du catalogue — cible du lot 1")
    a("=" * 60)

    a("")
    a("Ingrédients")
    pct_verif = (
        100 * rapport.ingredients_verifies / rapport.ingredients_total
        if rapport.ingredients_total
        else 0
    )
    a(f"  saisis            {_barre(rapport.ingredients_total, CIBLE_INGREDIENTS)} "
      f"{rapport.ingredients_total} / {CIBLE_INGREDIENTS}")
    a(f"  allergènes signés {_barre(rapport.ingredients_verifies, max(rapport.ingredients_total, 1))} "
      f"{rapport.ingredients_verifies} / {rapport.ingredients_total} ({pct_verif:.0f} %)")
    if rapport.ingredients_verifies < rapport.ingredients_total:
        manquants = rapport.ingredients_total - rapport.ingredients_verifies
        a(f"  ⚠ {manquants} ingrédient(s) non signé(s) — tout plat les employant est bloqué (FN-003)")

    a("")
    a("Plats")
    a(f"  publiés           {_barre(rapport.plats_publies, CIBLE_PLATS_PUBLIES)} "
      f"{rapport.plats_publies} / {CIBLE_PLATS_PUBLIES}")
    for statut, compte in sorted(rapport.plats_par_statut.items()):
        if statut != DishStatus.PUBLISHED.value:
            a(f"    {statut:<20} {compte}")

    a("")
    a("Répartition des plats publiés par type de repas")
    for type_repas, cible in CIBLE_PAR_TYPE_REPAS.items():
        actuel = rapport.plats_par_type_repas.get(type_repas.value, 0)
        a(f"  {type_repas.value:<12} {_barre(actuel, cible)} {actuel} / {cible}")
    snacks = rapport.plats_par_type_repas.get("snack", 0)
    a(f"  {'snack':<12} {'':28} {snacks} (pas de cible chiffrée)")

    a("")
    a(f"Couverture objectif × restriction × repas  (pool minimal : {POOL_MINIMAL_PAR_CRENEAU} plats)")
    vides = rapport.creneaux_vides
    insuffisants = rapport.creneaux_insuffisants
    total = len(rapport.creneaux)
    a(f"  {total} combinaisons évaluées")
    a(f"  ✗ {len(vides)} à zéro plat       — un utilisateur n'obtiendrait aucun résultat")
    a(f"  ⚠ {len(insuffisants)} sous le pool minimal — le générateur risque de se bloquer")
    a(f"  ✓ {total - len(vides) - len(insuffisants)} suffisantes")

    if detail_creneaux and vides:
        a("")
        a("Combinaisons à zéro plat")
        for c in vides:
            restriction = c.restriction.value if c.restriction else "(sans restriction)"
            a(f"  {c.objectif.value:<20} {restriction:<16} {c.type_repas.value}")

    a("")
    a("=" * 60)
    if rapport.pret_pour_le_lot_2:
        a("✓ Seuil du lot 2 atteint — le générateur peut être ouvert.")
    else:
        a("✗ Seuil du lot 2 non atteint. Il reste à :")
        if rapport.ingredients_total < CIBLE_INGREDIENTS:
            a(f"    · saisir {CIBLE_INGREDIENTS - rapport.ingredients_total} ingrédient(s)")
        if rapport.ingredients_verifies < rapport.ingredients_total:
            a(f"    · faire signer {rapport.ingredients_total - rapport.ingredients_verifies} "
              "grille(s) d'allergènes par le nutritionniste")
        if rapport.plats_publies < CIBLE_PLATS_PUBLIES:
            a(f"    · publier {CIBLE_PLATS_PUBLIES - rapport.plats_publies} plat(s)")
        if vides:
            a(f"    · couvrir {len(vides)} combinaison(s) restée(s) à zéro")

    return "\n".join(lignes)


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/couverture_catalogue.py",
        description="Avancement et couverture du catalogue (sprint 03).",
    )
    parser.add_argument("--json", action="store_true", help="sortie machine")
    parser.add_argument(
        "--creneaux",
        action="store_true",
        help="détailler les combinaisons à zéro plat",
    )
    args = parser.parse_args()

    plats, ingredients = _lire_catalogue()
    rapport = analyser(plats, ingredients)

    if args.json:
        print(
            json.dumps(
                {
                    "ingredients_total": rapport.ingredients_total,
                    "ingredients_verifies": rapport.ingredients_verifies,
                    "cible_ingredients": CIBLE_INGREDIENTS,
                    "plats_publies": rapport.plats_publies,
                    "cible_plats_publies": CIBLE_PLATS_PUBLIES,
                    "plats_par_statut": dict(rapport.plats_par_statut),
                    "plats_par_type_repas": dict(rapport.plats_par_type_repas),
                    "creneaux_vides": [
                        {
                            "objectif": c.objectif.value,
                            "restriction": c.restriction.value if c.restriction else None,
                            "type_repas": c.type_repas.value,
                        }
                        for c in rapport.creneaux_vides
                    ],
                    "creneaux_insuffisants": len(rapport.creneaux_insuffisants),
                    "pret_pour_le_lot_2": rapport.pret_pour_le_lot_2,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_rendre(rapport, detail_creneaux=args.creneaux))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
