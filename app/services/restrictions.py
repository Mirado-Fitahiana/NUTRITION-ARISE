"""Normalisation des restrictions personnalisées (FN-004).

La règle est courte et son respect est tout l'enjeu :

> Une restriction personnalisée est normalisée vers un ou plusieurs
> ingrédients ou tags connus lors de la saisie ; **si elle n'est pas
> normalisable, elle est signalée à l'administrateur et n'est pas appliquée
> silencieusement.**

Le danger d'une normalisation approximative est le même que celui d'une
conversion d'unité inventée (FN-012) : un utilisateur qui a écrit « pas de
fruits de mer » et à qui l'on sert des crevettes ne fait pas la différence
entre un bug et un mensonge. Aucune correspondance floue ici — l'appariement
est exact, sur le nom, le slug ou un alias, et tout ce qui échoue part en revue.

Ce module ne décide jamais qu'une restriction est *satisfaite* : il traduit une
phrase en identifiants. Le filtrage, lui, relève de FN-019.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

from app.models.enums import DishTag, RestrictionType

#: Restrictions dont le sens est porté par l'énumération elle-même : elles
#: n'ont rien à normaliser.
STRUCTURED_RESTRICTIONS: frozenset[RestrictionType] = frozenset(
    {
        RestrictionType.VEGETARIAN,
        RestrictionType.VEGAN,
        RestrictionType.NO_PORK,
        RestrictionType.NO_ALCOHOL,
        RestrictionType.LACTOSE_FREE,
        RestrictionType.GLUTEN_FREE,
    }
)

#: Correspondance directe d'un libellé courant vers un tag du catalogue.
#: Volontairement courte : chaque entrée est une affirmation vérifiable, pas
#: une heuristique. On l'étoffe quand un cas réel remonte de la revue.
LABEL_TO_TAG: dict[str, DishTag] = {
    "vegetarien": DishTag.VEGETARIAN,
    "vegetalien": DishTag.VEGAN,
    "vegan": DishTag.VEGAN,
    "sans lactose": DishTag.LACTOSE_FREE,
    "sans gluten": DishTag.GLUTEN_FREE,
}


def fold(text: str) -> str:
    """Minuscules, sans accent, espaces normalisés — pour comparer des libellés
    saisis à la main à des noms de catalogue."""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


@dataclass(frozen=True)
class NormalizationResult:
    """Ce qu'une restriction saisie devient — ou pourquoi elle ne devient rien."""

    is_normalized: bool
    needs_admin_review: bool
    ingredient_ids: tuple[UUID, ...] = ()
    tags: tuple[str, ...] = ()
    #: Termes que le catalogue ne connaît pas. Alimente l'écran de revue.
    unmatched: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class IngredientIndex:
    """Index d'appariement construit depuis le catalogue.

    Passé en argument plutôt que lu ici : ce module reste testable sans base,
    comme le reste de `app/services`.
    """

    #: `libellé replié` → identifiant. Contient noms, slugs et alias.
    by_label: dict[str, UUID]

    @classmethod
    def build(cls, rows: list[tuple[UUID, str, str, list[str]]]) -> "IngredientIndex":
        """`rows` : `(id, name, slug, aliases)` pour chaque ingrédient actif."""
        index: dict[str, UUID] = {}
        for ingredient_id, name, slug, aliases in rows:
            for label in (name, slug, *aliases):
                if label:
                    index.setdefault(fold(label), ingredient_id)
        return cls(by_label=index)

    def lookup(self, label: str) -> UUID | None:
        return self.by_label.get(fold(label))


def split_terms(label: str) -> list[str]:
    """Découpe « pas de crevettes, ni de crabe » en termes appariables.

    Les séparateurs sont explicites — virgule, point-virgule, « et », « ni »,
    « ou ». On ne tente rien de plus fin : mieux vaut envoyer en revue qu'apparier
    de travers.
    """
    cleaned = fold(label)
    for prefix in ("pas de ", "pas d'", "sans ", "aucun ", "aucune ", "ni "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    parts = re.split(r"[,;/]|\bet\b|\bni\b|\bou\b", cleaned)
    return [p.strip() for p in parts if p.strip()]


def normalize(
    restriction_type: RestrictionType,
    custom_label: str | None,
    index: IngredientIndex,
) -> NormalizationResult:
    """Traduit une restriction déclarée en identifiants exploitables.

    Une restriction structurée est normalisée d'office. Une restriction libre
    n'est normalisée que si **tous** ses termes sont appariés : un appariement
    partiel laisserait passer ce qui n'a pas été reconnu, ce qui est exactement
    l'application silencieuse que FN-004 interdit.
    """
    if restriction_type in STRUCTURED_RESTRICTIONS:
        return NormalizationResult(
            is_normalized=True,
            needs_admin_review=False,
            tags=(restriction_type.value,),
        )

    if not custom_label or not custom_label.strip():
        return NormalizationResult(
            is_normalized=False, needs_admin_review=True, unmatched=()
        )

    direct_tag = LABEL_TO_TAG.get(fold(custom_label))
    if direct_tag is not None:
        return NormalizationResult(
            is_normalized=True, needs_admin_review=False, tags=(direct_tag.value,)
        )

    matched: list[UUID] = []
    unmatched: list[str] = []
    for term in split_terms(custom_label):
        found = index.lookup(term)
        if found is None:
            unmatched.append(term)
        elif found not in matched:
            matched.append(found)

    if unmatched or not matched:
        # Signalée, pas appliquée. C'est la seule issue acceptable.
        return NormalizationResult(
            is_normalized=False,
            needs_admin_review=True,
            ingredient_ids=tuple(matched),
            unmatched=tuple(unmatched),
        )

    return NormalizationResult(
        is_normalized=True, needs_admin_review=False, ingredient_ids=tuple(matched)
    )


__all__ = [
    "LABEL_TO_TAG",
    "STRUCTURED_RESTRICTIONS",
    "IngredientIndex",
    "NormalizationResult",
    "fold",
    "normalize",
    "split_terms",
]
