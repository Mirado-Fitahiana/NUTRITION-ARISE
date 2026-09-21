"""Import de relevés de prix depuis un fichier — sprint 04, §4 et §5.

    python scripts/import_prix.py --gabarit releve.csv        # modèle de saisie
    python scripts/import_prix.py --depuis releve.csv --dry-run
    python scripts/import_prix.py --depuis releve.csv

Un opérateur qui revient du marché avec trente prix ne remplit pas trente
formulaires. Il remplit une ligne par relevé dans un tableur, et lance ce
script.

Deux règles héritées du reste du service :

* **`--dry-run` d'abord**, comme le chargeur de seed : on valide tout, on
  annonce ce qui serait écrit, puis seulement on écrit ;
* **appariement exact uniquement** (§5). La colonne `ingredient` accepte un
  slug, un nom ou un alias — mais jamais une approximation. Une ligne non
  appariée part en revue, elle n'est pas devinée.
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, selectinload  # noqa: E402

from app.core.database import create_sync_session_factory  # noqa: E402
from app.models.catalog import Ingredient, IngredientAlias  # noqa: E402
from app.models.enums import PriceSource  # noqa: E402
from app.models.pricing import IngredientPrice, Vendor  # noqa: E402
from app.services.pricing import (  # noqa: E402
    Observation,
    agreger,
    apparier_ingredient,
    confiance,
    est_aberrant,
    normaliser_libelle,
)

COLONNES = [
    "ingredient",
    "vendor_slug",
    "price",
    "unit",
    "quantity",
    "currency",
    "collected_at",
    "source",
    "note",
    "confirm_outlier",
]

EXEMPLE = {
    "ingredient": "riz-blanc-cru",
    "vendor_slug": "marche-analakely",
    "price": "3000",
    "unit": "kapoaka",
    "quantity": "1",
    "currency": "MGA",
    "collected_at": "2026-09-15",
    "source": "manual",
    "note": "",
    "confirm_outlier": "",
}


@dataclass
class Rejet:
    ligne: int
    libelle: str
    motif: str


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _booleen(valeur: str | None) -> bool:
    return bool(valeur) and valeur.strip().lower() in {"oui", "o", "true", "vrai", "1", "x"}


def _date(valeur: str) -> datetime:
    """Accepte `2026-09-15` comme `2026-09-15T08:00:00+03:00`.

    Une date nue est datée à midi UTC plutôt qu'à minuit : minuit bascule de
    jour au moindre décalage de fuseau, et la fraîcheur d'un relevé se joue au
    jour près.
    """
    texte = valeur.strip()
    if not texte:
        raise ValueError("date d'observation absente (FN-015)")
    try:
        if len(texte) == 10:
            return datetime.fromisoformat(texte).replace(hour=12, tzinfo=UTC)
        parsed = datetime.fromisoformat(texte)
    except ValueError as exc:
        raise ValueError(f"date illisible : {texte}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def construire_index(session: Session) -> dict[str, str]:
    """Libellés normalisés → slug. Inclut slugs, noms et alias (§5)."""
    index: dict[str, str] = {}
    ingredients = session.scalars(
        select(Ingredient).options(selectinload(Ingredient.aliases))
    ).all()
    for ingredient in ingredients:
        index[normaliser_libelle(ingredient.slug)] = ingredient.slug
        index[normaliser_libelle(ingredient.name)] = ingredient.slug
        for alias in ingredient.aliases:
            index.setdefault(normaliser_libelle(alias.alias), ingredient.slug)
    return index


def importer(chemin: Path, *, dry_run: bool, auteur: str) -> int:
    session_factory = create_sync_session_factory()
    with session_factory() as session:
        index = construire_index(session)
        par_slug = {i.slug: i for i in session.scalars(select(Ingredient)).all()}
        vendeurs = {v.slug: v for v in session.scalars(select(Vendor)).all()}

        with chemin.open(encoding="utf-8-sig", newline="") as flux:
            lignes = list(csv.DictReader(flux))

        rejets: list[Rejet] = []
        signales: list[Rejet] = []
        a_ecrire: list[IngredientPrice] = []
        maintenant = datetime.now(UTC)

        # Médianes chargées une fois : recalculer par ligne ferait autant de
        # requêtes que de relevés, pour un fichier qui en compte des centaines.
        medianes: dict[tuple[str, str], Decimal | None] = {}

        for numero, ligne in enumerate(lignes, start=2):
            libelle = (ligne.get("ingredient") or "").strip()
            slug = apparier_ingredient(libelle, index)
            if slug is None:
                rejets.append(Rejet(numero, libelle, "aucun ingrédient ne correspond exactement"))
                continue

            vendeur = vendeurs.get((ligne.get("vendor_slug") or "").strip())
            if vendeur is None:
                rejets.append(Rejet(numero, libelle, f"point de vente inconnu : {ligne.get('vendor_slug')}"))
                continue
            if not vendeur.is_active:
                rejets.append(Rejet(numero, libelle, "point de vente inactif"))
                continue

            try:
                prix = Decimal((ligne.get("price") or "").strip().replace(",", "."))
                quantite = Decimal((ligne.get("quantity") or "1").strip().replace(",", ".") or "1")
                collecte = _date(ligne.get("collected_at") or "")
            except (InvalidOperation, ValueError) as exc:
                rejets.append(Rejet(numero, libelle, str(exc)))
                continue

            if prix <= 0 or quantite <= 0:
                rejets.append(Rejet(numero, libelle, "prix et quantité doivent être strictement positifs"))
                continue

            unite = (ligne.get("unit") or "").strip()
            if not unite:
                rejets.append(Rejet(numero, libelle, "unité d'observation absente"))
                continue

            try:
                source = PriceSource((ligne.get("source") or "manual").strip() or "manual")
            except ValueError:
                rejets.append(Rejet(numero, libelle, f"source inconnue : {ligne.get('source')}"))
                continue

            ingredient = par_slug[slug]
            cle = (slug, unite)
            if cle not in medianes:
                existantes = session.scalars(
                    select(IngredientPrice).where(
                        IngredientPrice.ingredient_id == ingredient.id,
                        IngredientPrice.unit == unite,
                    )
                ).all()
                fourchette = agreger(
                    [
                        Observation(price=p.price, collected_at=p.collected_at, source=p.source, unit=p.unit)
                        for p in existantes
                    ]
                )
                medianes[cle] = fourchette.mediane if fourchette else None

            aberrant = est_aberrant(prix, medianes[cle])
            if aberrant and not _booleen(ligne.get("confirm_outlier")):
                rejets.append(
                    Rejet(
                        numero,
                        libelle,
                        f"écart de plus de 50 % avec la médiane connue ({medianes[cle]}) — "
                        "mettre confirm_outlier à « oui » pour confirmer",
                    )
                )
                continue
            if aberrant:
                signales.append(Rejet(numero, libelle, f"confirmé malgré l'écart ({medianes[cle]})"))

            a_ecrire.append(
                IngredientPrice(
                    id=uuid.uuid4(),
                    ingredient_id=ingredient.id,
                    vendor_id=vendeur.id,
                    price=prix,
                    currency=(ligne.get("currency") or "MGA").strip() or "MGA",
                    unit=unite,
                    quantity=quantite,
                    collected_at=collecte,
                    recorded_at=maintenant,
                    source=source,
                    collected_by=auteur,
                    confidence=confiance(
                        [Observation(price=prix, collected_at=collecte, source=source, unit=unite)],
                        maintenant=maintenant,
                    ),
                    outlier_confirmed=aberrant,
                    note=(ligne.get("note") or "").strip() or None,
                )
            )

        print(f"Lu : {len(lignes)} ligne(s)")
        print(f"  ✓ {len(a_ecrire)} relevé(s) valides")
        if signales:
            print(f"  ⚠ {len(signales)} aberration(s) confirmée(s) :")
            for s in signales:
                print(f"      ligne {s.ligne} · {s.libelle} · {s.motif}")
        if rejets:
            print(f"  ✗ {len(rejets)} refus :")
            for r in rejets:
                print(f"      ligne {r.ligne} · {r.libelle or '(vide)'} · {r.motif}")

        if dry_run:
            print("\n✓ validation seule — aucune écriture (--dry-run)")
            return 1 if rejets else 0

        for row in a_ecrire:
            session.add(row)
        session.commit()
        print(f"\n✓ {len(a_ecrire)} relevé(s) écrit(s)")
        return 1 if rejets else 0


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/import_prix.py",
        description="Importe des relevés de prix depuis un CSV (sprint 04).",
    )
    parser.add_argument("--gabarit", type=Path, help="écrit un modèle CSV à ce chemin")
    parser.add_argument("--depuis", type=Path, help="fichier CSV à importer")
    parser.add_argument("--dry-run", action="store_true", help="valide sans écrire")
    parser.add_argument(
        "--auteur",
        default="import:csv",
        help="identifiant du contributeur enregistré sur chaque relevé",
    )
    args = parser.parse_args()

    if args.gabarit:
        args.gabarit.parent.mkdir(parents=True, exist_ok=True)
        with args.gabarit.open("w", encoding="utf-8-sig", newline="") as flux:
            writer = csv.DictWriter(flux, fieldnames=COLONNES)
            writer.writeheader()
            writer.writerow(EXEMPLE)
        print(f"✓ {args.gabarit}")
        print(f"\n  python scripts/import_prix.py --depuis {args.gabarit} --dry-run")
        return 0

    if not args.depuis:
        parser.error("indiquer --gabarit ou --depuis")
    if not args.depuis.exists():
        print(f"✗ fichier introuvable : {args.depuis}", file=sys.stderr)
        return 2

    return importer(args.depuis, dry_run=args.dry_run, auteur=args.auteur)


if __name__ == "__main__":
    raise SystemExit(main())
