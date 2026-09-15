"""Schémas d'entrée du banc d'essai interne (FN-034).

Ces modèles ne décrivent pas l'API publique du service : ils décrivent les
formulaires de la console de test. Ils sont donc volontairement plus permissifs
que ne le sera l'API — la console doit pouvoir **provoquer** les erreurs
(quantité nulle, conversion absente, portions invalides), pas seulement
exécuter les cas passants.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import Allergen, ReferenceUnit, RestrictionFlag


class ConsoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConversionIn(ConsoleModel):
    """FN-012 — facteur mesuré, propre à un ingrédient."""

    from_unit: str = Field(min_length=1, max_length=40)
    to_unit: str = Field(min_length=1, max_length=40)
    factor: Decimal = Field(gt=0)


class UnitConvertIn(ConsoleModel):
    """Une conversion isolée, telle que `to_reference_quantity` la reçoit."""

    # `quantity` n'est pas bornée : une quantité nulle ou négative doit remonter
    # comme `UnitConversionError`, et c'est précisément un cas à éprouver.
    quantity: Decimal
    unit: str = Field(min_length=1, max_length=40)
    reference_unit: ReferenceUnit
    density_g_per_ml: Decimal | None = Field(default=None, gt=0)
    conversions: list[ConversionIn] = Field(default_factory=list)


class CustomIngredientIn(ConsoleModel):
    """Ingrédient saisi à la main, pour éprouver un cas absent du catalogue.

    C'est le seul moyen, tant que le catalogue de démarrage n'est pas signé, de
    vérifier le chemin « allergènes vérifiés » de FN-003 : il suffit de basculer
    `is_allergen_verified` et d'observer le verdict de publication changer.
    """

    slug: str = Field(default="ingredient-libre", min_length=1, max_length=200)
    reference_unit: ReferenceUnit = ReferenceUnit.G
    kcal_100: Decimal = Field(ge=0)
    protein_100: Decimal = Field(ge=0)
    carbs_100: Decimal = Field(ge=0)
    fat_100: Decimal = Field(ge=0)
    fiber_100: Decimal = Field(default=Decimal("0"), ge=0)
    allergens: list[Allergen] = Field(default_factory=list)
    restriction_flags: list[RestrictionFlag] = Field(default_factory=list)
    density_g_per_ml: Decimal | None = Field(default=None, gt=0)
    conversions: list[ConversionIn] = Field(default_factory=list)
    is_allergen_verified: bool = False


class ComponentIn(ConsoleModel):
    """Un ingrédient et sa quantité dans le plat testé.

    Deux origines possibles, exclusives : un `slug` du catalogue versionné, ou
    un ingrédient entièrement saisi.
    """

    ingredient: str | None = None
    custom: CustomIngredientIn | None = None
    quantity: Decimal
    unit: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ComponentIn":
        if (self.ingredient is None) == (self.custom is None):
            raise ValueError(
                "renseigner soit `ingredient` (slug du catalogue), soit `custom`"
            )
        return self


class DeriveIn(ConsoleModel):
    """Entrée de `derive_dish_data` : une composition et un nombre de portions."""

    # Non bornée à dessein : `compute_nutrition` refuse `servings <= 0`, et ce
    # refus fait partie de ce que la console doit pouvoir montrer.
    servings: int = 1
    components: list[ComponentIn] = Field(default_factory=list)


__all__ = [
    "ComponentIn",
    "ConversionIn",
    "CustomIngredientIn",
    "DeriveIn",
    "UnitConvertIn",
]


class DevTokenIn(ConsoleModel):
    """Jeton de développement à émettre depuis le banc d'essai.

    Les quatre derniers champs servent à produire des jetons **volontairement
    mauvais** : sans eux, on ne peut vérifier que le chemin passant, et un
    contrôle de sécurité qu'on n'a jamais vu refuser n'est pas un contrôle
    vérifié.
    """

    sub: str = Field(default="dev-user-001", min_length=1, max_length=128)
    role: str = Field(default="user", max_length=32)
    entitlements: list[str] = Field(default_factory=lambda: ["nutrition"])

    #: Signe en HS256 avec un secret arbitraire — doit être refusé (D-03).
    force_hs256: bool = False
    #: Remplace l'audience attendue — doit être refusé.
    wrong_audience: bool = False
    #: Remplace l'émetteur attendu — doit être refusé.
    wrong_issuer: bool = False
    #: Émet un jeton déjà expiré — doit être refusé.
    expired: bool = False
