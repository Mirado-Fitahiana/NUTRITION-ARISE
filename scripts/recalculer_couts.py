"""Matérialisation des coûts de plats — sprint 06, §4.

    python scripts/recalculer_couts.py --dry-run
    python scripts/recalculer_couts.py
    python scripts/recalculer_couts.py --vendeur kibo-tananarive

Le coût d'un plat change quand les **prix** changent, pas quand le plat change.
Le recalculer à la volée sur un catalogue entier à chaque requête serait payer
en permanence pour une donnée qui bouge une fois par semaine. On le matérialise
donc dans `dish_cost_estimates`, et on relance ce script après chaque import de
prix, ou par tâche planifiée.

Comme le chargeur de seed, `--dry-run` montre ce qui serait écrit sans écrire.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, selectinload  # noqa: E402

from app.core.database import create_sync_session_factory  # noqa: E402
from app.models.catalog import Dish, DishIngredient, Ingredient  # noqa: E402
from app.models.pricing import DishCostEstimate, IngredientPrice, Vendor  # noqa: E402
from app.services.pricing import (  # noqa: E402
    CoutStatut,
    ReleveCandidat,
    cout_ingredient,
    cout_plat,
)
from app.services.units import IngredientConversion  # noqa: E402


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _candidats(session: Session) -> dict[uuid.UUID, list[ReleveCandidat]]:
    par_ingredient: dict[uuid.UUID, list[ReleveCandidat]] = {}
    for prix, vendeur in session.execute(
        select(IngredientPrice, Vendor).join(Vendor, Vendor.id == IngredientPrice.vendor_id)
    ).all():
        par_ingredient.setdefault(prix.ingredient_id, []).append(
            ReleveCandidat(
                vendor_slug=vendeur.slug,
                vendor_zone=vendeur.zone,
                price=prix.price,
                unit=prix.unit,
                quantity=prix.quantity,
                collected_at=prix.collected_at,
                source=prix.source,
            )
        )
    return par_ingredient


def recalculer(*, dry_run: bool, vendeur: str | None, zone: str | None) -> int:
    session_factory = create_sync_session_factory()
    with session_factory() as session:
        plats = list(
            session.scalars(
                select(Dish).options(
                    selectinload(Dish.ingredients)
                    .selectinload(DishIngredient.ingredient)
                    .selectinload(Ingredient.unit_conversions)
                )
            )
        )
        if not plats:
            print("Aucun plat au catalogue — rien à chiffrer.")
            return 0

        candidats = _candidats(session)
        maintenant = datetime.now(UTC)

        vendor_id: uuid.UUID | None = None
        if vendeur:
            vendeur_row = session.scalar(select(Vendor).where(Vendor.slug == vendeur))
            if vendeur_row is None:
                print(f"✗ point de vente inconnu : {vendeur}", file=sys.stderr)
                return 2
            vendor_id = vendeur_row.id

        existants = {
            (e.dish_id, e.vendor_id): e
            for e in session.scalars(select(DishCostEstimate)).all()
        }

        par_statut: dict[str, int] = {}
        ecrits = 0

        for dish in plats:
            lignes = [
                cout_ingredient(
                    composant.ingredient.slug,
                    composant.quantity,
                    composant.unit,
                    composant.ingredient.reference_unit,
                    candidats.get(composant.ingredient_id, []),
                    density_g_per_ml=composant.ingredient.density_g_per_ml,
                    conversions=[
                        IngredientConversion(c.from_unit, c.to_unit, c.factor)
                        for c in composant.ingredient.unit_conversions
                    ],
                    vendeur_prefere=vendeur,
                    zone=zone,
                    maintenant=maintenant,
                )
                for composant in dish.ingredients
            ]
            cout = cout_plat(lignes, dish.servings, maintenant=maintenant)
            par_statut[str(cout.statut)] = par_statut.get(str(cout.statut), 0) + 1

            manquants = cout.ingredients_sans_prix
            detail = f" · {len(manquants)} sans prix" if manquants else ""
            montant = (
                f"{cout.cout_portion_median} Ar/portion"
                if cout.cout_portion_median is not None
                else "non chiffrable"
            )
            print(
                f"  {dish.slug:<28} {str(cout.statut):<12} "
                f"couverture {cout.couverture:>6} · {montant}{detail}"
            )

            if dry_run:
                continue

            # Idempotent : un plat déjà chiffré pour ce périmètre est mis à jour,
            # jamais dupliqué.
            ligne = existants.get((dish.id, vendor_id))
            if ligne is None:
                ligne = DishCostEstimate(
                    id=uuid.uuid4(), dish_id=dish.id, vendor_id=vendor_id,
                    coverage_ratio=Decimal("0"), computed_at=maintenant,
                )
                session.add(ligne)
            ligne.total_cost_min = cout.cout_total_min
            ligne.total_cost_median = cout.cout_total_median
            ligne.total_cost_max = cout.cout_total_max
            ligne.coverage_ratio = cout.couverture
            ligne.oldest_observation_at = cout.observation_la_plus_ancienne
            ligne.computed_at = maintenant
            ecrits += 1

        print()
        print(f"{len(plats)} plat(s) évalué(s) — " + " · ".join(
            f"{compte} {statut}" for statut, compte in sorted(par_statut.items())
        ))

        if dry_run:
            print("\n✓ simulation — aucune écriture (--dry-run)")
            return 0

        session.commit()
        print(f"\n✓ {ecrits} estimation(s) écrite(s) dans dish_cost_estimates")
        return 0


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/recalculer_couts.py",
        description="Recalcule et matérialise le coût des plats (sprint 06).",
    )
    parser.add_argument("--dry-run", action="store_true", help="montre sans écrire")
    parser.add_argument(
        "--vendeur",
        help="chiffrer pour ce point de vente (défaut : toutes zones confondues)",
    )
    parser.add_argument("--zone", help="repli sur cette zone")
    args = parser.parse_args()

    return recalculer(dry_run=args.dry_run, vendeur=args.vendeur, zone=args.zone)


if __name__ == "__main__":
    raise SystemExit(main())
