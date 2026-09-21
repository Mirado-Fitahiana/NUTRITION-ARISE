"""Règles de prix — fraîcheur, agrégation, confiance (FN-015, FN-016, D-09).

Fonctions **pures**, sans base : elles se testent en millisecondes, comme
`nutrition.py` et `units.py`. C'est délibéré. Un prix périmé qui se glisse dans
un budget, ou une médiane calculée à l'envers, ne produit pas une erreur visible
— il produit une liste de courses fausse que personne ne remarque.

Trois règles portent tout le module :

* **D-09 — jamais un prix unique.** Un marché négocié et volatil ne permet pas
  de promettre un prix ; on expose une fourchette (p10 / médiane / p90) avec sa
  date et sa confiance.
* **FN-016 — un prix obsolète est exclu du budget.** Au-delà de 45 jours, il ne
  sert plus à chiffrer : il est écarté, pas pondéré.
* **FN-015 — un écart de plus de 50 % par rapport à la médiane connue demande
  confirmation.** La saisie n'est pas refusée : elle est signalée. Un prix
  aberrant est parfois le vrai prix.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.models.enums import PriceFreshness, PriceSource, ReferenceUnit
from app.services.units import (
    IngredientConversion,
    UnitConversionError,
    to_reference_quantity,
)

#: FN-016 — seuils par défaut, en jours. **Configurables** : ils vivront dans
#: `app_settings`. Les valeurs ici sont le repli quand rien n'est configuré.
SEUIL_RECENT_JOURS = 14
SEUIL_ANCIEN_JOURS = 45

#: FN-015 — au-delà de cet écart relatif à la médiane connue, la saisie
#: demande confirmation au contributeur.
SEUIL_ABERRATION = Decimal("0.5")

#: Pondération de confiance par méthode de collecte (D-08). Le relevé manuel
#: est la référence : c'est le seul mécanisme garanti de fonctionner (FN-015).
CONFIANCE_PAR_SOURCE: dict[PriceSource, Decimal] = {
    PriceSource.MANUAL: Decimal("1.0"),
    PriceSource.OFFICIAL: Decimal("0.9"),
    PriceSource.SCRAPING: Decimal("0.7"),
    PriceSource.COMMUNITY: Decimal("0.6"),
}


@dataclass(frozen=True)
class Observation:
    """Un relevé réduit à ce dont l'agrégation a besoin."""

    price: Decimal
    collected_at: datetime
    source: PriceSource = PriceSource.MANUAL
    unit: str = "g"


@dataclass(frozen=True)
class Fourchette:
    """D-09 — le seul format sous lequel un prix est exposé."""

    minimum: Decimal
    mediane: Decimal
    maximum: Decimal
    observed_at: datetime
    freshness: PriceFreshness
    confidence: Decimal
    observations: int

    @property
    def utilisable_pour_budget(self) -> bool:
        """FN-016 — un prix obsolète est exclu du chiffrage."""
        return self.freshness in (PriceFreshness.RECENT, PriceFreshness.STALE)


def _maintenant(reference: datetime | None) -> datetime:
    return reference or datetime.now(UTC)


def _round(value: Decimal) -> Decimal:
    """Même arrondi que le calcul nutritionnel — `ROUND_HALF_UP`, 2 décimales."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fraicheur(
    collected_at: datetime,
    *,
    maintenant: datetime | None = None,
    seuil_recent: int = SEUIL_RECENT_JOURS,
    seuil_ancien: int = SEUIL_ANCIEN_JOURS,
) -> PriceFreshness:
    """FN-016 — `récent` ≤ 14 j · `ancien` 15-45 j · `obsolète` > 45 j.

    Une date d'observation dans le futur est traitée comme `récent` : c'est une
    faute de saisie, pas une donnée périmée, et l'écarter silencieusement la
    rendrait invisible.
    """
    ecart = _maintenant(maintenant) - collected_at
    if ecart <= timedelta(days=seuil_recent):
        return PriceFreshness.RECENT
    if ecart <= timedelta(days=seuil_ancien):
        return PriceFreshness.STALE
    return PriceFreshness.OBSOLETE


def _percentile(valeurs: list[Decimal], fraction: float) -> Decimal:
    """Percentile par interpolation linéaire, sur une liste **déjà triée**."""
    if not valeurs:
        raise ValueError("percentile sur une liste vide")
    if len(valeurs) == 1:
        return valeurs[0]

    position = fraction * (len(valeurs) - 1)
    bas = int(position)
    haut = min(bas + 1, len(valeurs) - 1)
    poids = Decimal(str(position - bas))
    return valeurs[bas] + (valeurs[haut] - valeurs[bas]) * poids


def mediane(valeurs: list[Decimal]) -> Decimal:
    return _percentile(sorted(valeurs), 0.5)


def confiance(
    observations: list[Observation], *, maintenant: datetime | None = None
) -> Decimal:
    """FN-016 — `f(nombre d'observations, âge, méthode)`, borné à [0, 1].

    Trois observations récentes valent mieux qu'une, mais dix n'apportent pas
    dix fois plus : la composante volume sature. Une seule observation plafonne
    volontairement bas — elle ne dit rien de la dispersion.
    """
    if not observations:
        return Decimal("0")

    # Volume : 1 → 0.5, 2 → 0.75, 3 → 0.83, sature vers 1.
    volume = Decimal(1) - Decimal(1) / Decimal(1 + len(observations))

    reference = _maintenant(maintenant)
    ages = [(reference - o.collected_at).days for o in observations]
    age_moyen = max(0, sum(ages) // len(ages))
    if age_moyen <= SEUIL_RECENT_JOURS:
        recence = Decimal("1.0")
    elif age_moyen <= SEUIL_ANCIEN_JOURS:
        recence = Decimal("0.6")
    else:
        recence = Decimal("0.2")

    methode = min(CONFIANCE_PAR_SOURCE.get(o.source, Decimal("0.5")) for o in observations)

    score = volume * recence * methode
    return min(Decimal("1"), max(Decimal("0"), score)).quantize(Decimal("0.01"))


def agreger(
    observations: list[Observation],
    *,
    maintenant: datetime | None = None,
    inclure_obsoletes: bool = False,
) -> Fourchette | None:
    """D-09 / FN-016 — construit la fourchette exposée au client.

    Renvoie `None` quand il ne reste rien à agréger : **pas** une fourchette à
    zéro, qui se lirait comme un prix gratuit. L'absence de prix est une
    information en soi, et l'appelant doit la traiter comme telle.

    Les observations obsolètes sont écartées par défaut (FN-016). Les inclure
    reste possible pour un écran d'administration qui montre l'historique.
    """
    if not observations:
        return None

    reference = _maintenant(maintenant)
    retenues = observations
    if not inclure_obsoletes:
        retenues = [
            o
            for o in observations
            if fraicheur(o.collected_at, maintenant=reference) != PriceFreshness.OBSOLETE
        ]
    if not retenues:
        return None

    prix = sorted(o.price for o in retenues)
    plus_recente = max(o.collected_at for o in retenues)

    return Fourchette(
        minimum=_percentile(prix, 0.10),
        mediane=_percentile(prix, 0.50),
        maximum=_percentile(prix, 0.90),
        observed_at=plus_recente,
        freshness=fraicheur(plus_recente, maintenant=reference),
        confidence=confiance(retenues, maintenant=reference),
        observations=len(retenues),
    )


def prix_par_unite_de_reference(
    prix: Decimal,
    quantite: Decimal,
    unite: str,
    reference_unit: ReferenceUnit,
    *,
    density_g_per_ml: Decimal | None = None,
    conversions: list[IngredientConversion] | None = None,
) -> Decimal:
    """Ramène un prix observé au prix de 100 unités de référence.

    Un prix se relève au kapoaka ou à la botte ; le calcul nutritionnel et le
    chiffrage raisonnent en unité de référence. La conversion passe donc par
    `units.to_reference_quantity`, qui **lève `UnitConversionError` quand le
    facteur manque**.

    Cette erreur n'est délibérément **pas** rattrapée ici : un kapoaka converti
    « au jugé » produirait un budget faux et crédible, ce qui est pire qu'un
    budget absent. Elle doit remonter jusqu'à l'appelant, qui affiche le manque.
    """
    quantite_reference = to_reference_quantity(
        quantite,
        unite,
        reference_unit,
        density_g_per_ml=density_g_per_ml,
        conversions=conversions,
    )
    if quantite_reference <= 0:
        raise UnitConversionError(unite, reference_unit.value, "quantité convertie nulle")
    return (prix / quantite_reference) * Decimal(100)


def apparier_ingredient(
    libelle: str, index: dict[str, str]
) -> str | None:
    """Rapproche un libellé de prix d'un `slug` d'ingrédient.

    **Appariement exact uniquement** — c'est la doctrine déjà appliquée par
    `services.restrictions`. Un rapprochement flou entre « riz rouge » et « riz
    blanc » produirait un prix faux sans que personne ne le voie ; en cas
    d'échec, la ligne part en revue manuelle plutôt qu'en devinette.

    `index` associe des libellés déjà normalisés (nom, alias) à leur slug.
    """
    cle = normaliser_libelle(libelle)
    return index.get(cle)


def normaliser_libelle(libelle: str) -> str:
    """Normalisation **conservatrice** : casse, espaces et accents.

    Volontairement limitée : elle ne retire ni mot ni qualificatif. Supprimer
    « rouge » ou « complet » pour élargir les correspondances reviendrait à
    faire de l'appariement flou par la petite porte.
    """
    sans_accent = unicodedata.normalize("NFKD", libelle)
    sans_accent = "".join(c for c in sans_accent if not unicodedata.combining(c))
    return " ".join(sans_accent.lower().split())


def est_aberrant(
    prix: Decimal,
    mediane_connue: Decimal | None,
    *,
    seuil: Decimal = SEUIL_ABERRATION,
) -> bool:
    """FN-015 — l'écart qui déclenche une demande de confirmation.

    Sans médiane connue, rien n'est aberrant : le premier prix d'un ingrédient
    n'a rien à quoi se comparer.
    """
    if mediane_connue is None or mediane_connue <= 0:
        return False
    return abs(prix - mediane_connue) / mediane_connue > seuil


# --------------------------------------------------------------------------
# FN-017 — coût d'un plat (sprint 06)
# --------------------------------------------------------------------------

#: Sous ce taux de couverture, un plat est déclaré **non chiffrable**. Donner
#: un coût calculé sur la moitié des ingrédients serait plus trompeur que ne
#: rien donner : le chiffre paraîtrait précis et serait faux par construction.
SEUIL_COUVERTURE_CHIFFRABLE = Decimal("0.7")


class CoutStatut(StrEnum):
    """Statut global du chiffrage d'un plat."""

    COMPLET = "complete"
    PARTIEL = "partial"
    INDISPONIBLE = "unavailable"


@dataclass(frozen=True)
class ReleveCandidat:
    """Une observation, enrichie de son vendeur — de quoi appliquer la règle
    de sélection sans interroger la base."""

    vendor_slug: str
    vendor_zone: str
    price: Decimal
    unit: str
    quantity: Decimal
    collected_at: datetime
    source: PriceSource = PriceSource.MANUAL


@dataclass(frozen=True)
class PerimetreRetenu:
    """D'où sort le chiffre. Exposé au client : l'utilisateur doit pouvoir
    comprendre sur quoi repose le coût qu'on lui annonce."""

    #: `vendor` · `zone` · `official`
    portee: str
    vendor_slug: str | None
    observed_at: datetime
    source: PriceSource
    observations: int


@dataclass(frozen=True)
class CoutIngredient:
    """Le coût d'une ligne, ou la raison précise de son absence."""

    ingredient_slug: str
    cout_min: Decimal | None = None
    cout_median: Decimal | None = None
    cout_max: Decimal | None = None
    retenu: PerimetreRetenu | None = None
    freshness: PriceFreshness = PriceFreshness.UNAVAILABLE
    #: Renseigné dès que la ligne n'est pas chiffrable. Jamais `None` en même
    #: temps qu'un coût absent : un manque sans motif est un manque inexploitable.
    motif: str | None = None

    @property
    def chiffre(self) -> bool:
        return self.cout_median is not None


@dataclass(frozen=True)
class CoutPlat:
    """FN-017 — le coût d'un plat, et **ce qu'on ne sait pas**.

    `couverture` n'est pas optionnelle. C'est elle qui empêche de présenter le
    coût de cinq ingrédients sur huit comme le coût du plat.
    """

    statut: CoutStatut
    couverture: Decimal
    lignes: tuple[CoutIngredient, ...]
    cout_total_min: Decimal | None = None
    cout_total_median: Decimal | None = None
    cout_total_max: Decimal | None = None
    cout_portion_min: Decimal | None = None
    cout_portion_median: Decimal | None = None
    cout_portion_max: Decimal | None = None
    #: L'observation la plus ancienne ayant servi : c'est elle qui détermine la
    #: fraîcheur de l'ensemble. Un plat n'est pas plus frais que son ingrédient
    #: le plus périmé.
    observation_la_plus_ancienne: datetime | None = None
    freshness: PriceFreshness = PriceFreshness.UNAVAILABLE

    @property
    def ingredients_sans_prix(self) -> tuple[str, ...]:
        return tuple(ligne.ingredient_slug for ligne in self.lignes if not ligne.chiffre)


def choisir_perimetre(
    candidats: list[ReleveCandidat],
    *,
    vendeur_prefere: str | None = None,
    zone: str | None = None,
    maintenant: datetime | None = None,
) -> list[ReleveCandidat]:
    """§3 — quel jeu de relevés sert au chiffrage, dans un ordre traçable.

    Le relevé du vendeur demandé d'abord ; à défaut celui de la zone ; à défaut
    la série officielle. On ne **mélange jamais** les périmètres : additionner
    un prix de Tananarive et un indice national donnerait un total qui ne
    correspond à aucun panier réel.

    Les observations obsolètes sont écartées à chaque étape (FN-016).
    """
    reference = _maintenant(maintenant)
    vivants = [
        c
        for c in candidats
        if fraicheur(c.collected_at, maintenant=reference) != PriceFreshness.OBSOLETE
    ]
    if not vivants:
        return []

    if vendeur_prefere:
        chez_le_vendeur = [c for c in vivants if c.vendor_slug == vendeur_prefere]
        if chez_le_vendeur:
            return chez_le_vendeur

    if zone:
        dans_la_zone = [
            c
            for c in vivants
            if c.vendor_zone == zone and c.source != PriceSource.OFFICIAL
        ]
        if dans_la_zone:
            return dans_la_zone

    officiels = [c for c in vivants if c.source == PriceSource.OFFICIAL]
    if officiels:
        return officiels

    # Aucun périmètre privilégié ne s'applique : tous les relevés de commerçants.
    return [c for c in vivants if c.source != PriceSource.OFFICIAL] or officiels


def _portee(candidats: list[ReleveCandidat], vendeur_prefere: str | None, zone: str | None) -> str:
    slugs = {c.vendor_slug for c in candidats}
    if vendeur_prefere and slugs == {vendeur_prefere}:
        return "vendor"
    if all(c.source == PriceSource.OFFICIAL for c in candidats):
        return "official"
    return "zone" if zone else "all"


def cout_ingredient(
    slug: str,
    quantite: Decimal,
    unite: str,
    reference_unit: ReferenceUnit,
    candidats: list[ReleveCandidat],
    *,
    density_g_per_ml: Decimal | None = None,
    conversions: list[IngredientConversion] | None = None,
    vendeur_prefere: str | None = None,
    zone: str | None = None,
    maintenant: datetime | None = None,
) -> CoutIngredient:
    """Coût d'un ingrédient dans un plat, ou le motif de son absence.

    **Aucune extrapolation.** Si l'ingrédient n'a pas de prix, ou si son unité
    n'est pas convertible, la ligne n'est pas chiffrée — on ne déduit jamais son
    prix d'un autre ingrédient.
    """
    reference = _maintenant(maintenant)
    retenus = choisir_perimetre(
        candidats, vendeur_prefere=vendeur_prefere, zone=zone, maintenant=reference
    )
    if not retenus:
        motif = (
            "aucun relevé de prix" if not candidats else "tous les relevés sont obsolètes"
        )
        return CoutIngredient(ingredient_slug=slug, motif=motif)

    # Le prix est relevé au kapoaka ou au kilo ; la recette s'exprime en
    # grammes. Les deux conversions passent par `units.py`, qui bloque plutôt
    # que d'inventer un facteur.
    try:
        quantite_recette = to_reference_quantity(
            quantite,
            unite,
            reference_unit,
            density_g_per_ml=density_g_per_ml,
            conversions=conversions,
        )
    except UnitConversionError as exc:
        return CoutIngredient(
            ingredient_slug=slug,
            motif=f"quantité de la recette non convertible : {exc.reason}",
        )

    prix_aux_100 : list[Decimal] = []
    for releve in retenus:
        try:
            prix_aux_100.append(
                prix_par_unite_de_reference(
                    releve.price,
                    releve.quantity,
                    releve.unit,
                    reference_unit,
                    density_g_per_ml=density_g_per_ml,
                    conversions=conversions,
                )
            )
        except UnitConversionError:
            # Un relevé dans une unité non convertible est ignoré, pas fatal :
            # un autre vendeur peut l'avoir relevé au kilo.
            continue

    if not prix_aux_100:
        return CoutIngredient(
            ingredient_slug=slug,
            motif="aucun relevé dans une unité convertible vers l'unité de référence",
        )

    facteur = quantite_recette / Decimal(100)
    tries = sorted(prix_aux_100)
    plus_recent = max(r.collected_at for r in retenus)

    return CoutIngredient(
        ingredient_slug=slug,
        cout_min=_round(_percentile(tries, 0.10) * facteur),
        cout_median=_round(_percentile(tries, 0.50) * facteur),
        cout_max=_round(_percentile(tries, 0.90) * facteur),
        retenu=PerimetreRetenu(
            portee=_portee(retenus, vendeur_prefere, zone),
            vendor_slug=retenus[0].vendor_slug if len({r.vendor_slug for r in retenus}) == 1 else None,
            observed_at=plus_recent,
            source=retenus[0].source,
            observations=len(prix_aux_100),
        ),
        freshness=fraicheur(plus_recent, maintenant=reference),
    )


def cout_plat(
    lignes: list[CoutIngredient],
    servings: int,
    *,
    seuil_couverture: Decimal = SEUIL_COUVERTURE_CHIFFRABLE,
    maintenant: datetime | None = None,
) -> CoutPlat:
    """FN-017 — agrège les lignes en un coût de plat, statut compris.

    Sous le seuil de couverture, le total est **retiré** — pas seulement
    signalé. Laisser un chiffre à côté d'un avertissement, c'est garantir que
    le chiffre sera lu et l'avertissement ignoré.
    """
    if not lignes:
        return CoutPlat(
            statut=CoutStatut.INDISPONIBLE, couverture=Decimal("0"), lignes=()
        )

    chiffrees = [l for l in lignes if l.chiffre]
    couverture = (Decimal(len(chiffrees)) / Decimal(len(lignes))).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )

    if not chiffrees:
        return CoutPlat(
            statut=CoutStatut.INDISPONIBLE,
            couverture=couverture,
            lignes=tuple(lignes),
        )

    plus_ancienne = min(
        l.retenu.observed_at for l in chiffrees if l.retenu is not None
    )
    etat = fraicheur(plus_ancienne, maintenant=_maintenant(maintenant))

    if couverture < seuil_couverture:
        return CoutPlat(
            statut=CoutStatut.INDISPONIBLE,
            couverture=couverture,
            lignes=tuple(lignes),
            observation_la_plus_ancienne=plus_ancienne,
            freshness=etat,
        )

    total_min = sum((l.cout_min for l in chiffrees), Decimal("0"))
    total_median = sum((l.cout_median for l in chiffrees), Decimal("0"))
    total_max = sum((l.cout_max for l in chiffrees), Decimal("0"))
    portions = Decimal(max(1, servings))

    return CoutPlat(
        statut=CoutStatut.COMPLET if len(chiffrees) == len(lignes) else CoutStatut.PARTIEL,
        couverture=couverture,
        lignes=tuple(lignes),
        cout_total_min=_round(total_min),
        cout_total_median=_round(total_median),
        cout_total_max=_round(total_max),
        cout_portion_min=_round(total_min / portions),
        cout_portion_median=_round(total_median / portions),
        cout_portion_max=_round(total_max / portions),
        observation_la_plus_ancienne=plus_ancienne,
        freshness=etat,
    )


__all__ = [
    "CONFIANCE_PAR_SOURCE",
    "CoutIngredient",
    "CoutPlat",
    "CoutStatut",
    "Fourchette",
    "Observation",
    "PerimetreRetenu",
    "ReleveCandidat",
    "SEUIL_ABERRATION",
    "SEUIL_ANCIEN_JOURS",
    "SEUIL_COUVERTURE_CHIFFRABLE",
    "SEUIL_RECENT_JOURS",
    "agreger",
    "apparier_ingredient",
    "choisir_perimetre",
    "confiance",
    "cout_ingredient",
    "cout_plat",
    "est_aberrant",
    "fraicheur",
    "mediane",
    "normaliser_libelle",
    "prix_par_unite_de_reference",
]
