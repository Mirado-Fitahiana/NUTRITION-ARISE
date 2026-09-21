"""SPIKE-01 mesuré — les quatre critères du §6.5, source par source.

Même règle que `app.services.progress` : **aucun critère ne passe au vert par
déclaration.** Chacun est déduit des relevés réellement archivés, et un critère
qu'on ne sait pas mesurer est `inconnu`, jamais `fait`.

Un écart assumé avec `scripts/spike01_releve.py --rapport` : le script compte les
trois derniers relevés exploitables, **quelle que soit leur date**. La
spécification dit « trois relevés espacés d'une semaine ». Ici, deux relevés à
moins de six jours d'intervalle comptent pour un seul — relancer le relevé trois
fois le même après-midi ne doit pas instruire le critère. Sur l'archive actuelle
(un relevé par source), les deux lectures rendent le même verdict.

Fonctions pures : la lecture de la base reste à l'appelant.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

FAIT, PARTIEL, A_FAIRE, INCONNU = "fait", "partiel", "afaire", "inconnu"

#: Sources qualifiées nécessaires à l'ouverture du lot 4 (§6.5).
SOURCES_REQUISES = 2
#: Critère 2 — ingrédients du référentiel couverts par une source.
INGREDIENTS_REQUIS = 30
#: Critère 3 — nombre de relevés hebdomadaires stables.
RELEVES_REQUIS = 3
#: « Espacés d'une semaine », avec un jour de tolérance.
ECART_MINIMAL = timedelta(days=6)


@dataclass(frozen=True)
class Releve:
    """Une exécution de collecte réduite à ce que la qualification lit."""

    horodatage: datetime
    mode: str  # structure | collecte
    reussi: bool
    produits: int = 0
    prix_trouves: int = 0
    exemples_prix: tuple[str, ...] = ()
    robots_autorise: bool | None = None
    erreur: str | None = None
    ingredients_apparies: int | None = None

    @property
    def exploitable(self) -> bool:
        return self.reussi and self.erreur is None and self.prix_trouves > 0


@dataclass(frozen=True)
class EtatSource:
    slug: str
    nom: str
    actif: bool
    releves: tuple[Releve, ...]
    cgu_attestees_par: str | None = None
    cgu_attestees_le: datetime | None = None
    cgu_lien: str | None = None


@dataclass
class Critere:
    numero: int
    libelle: str
    etat: str
    preuve: str
    prochaine_etape: str | None = None


@dataclass
class QualificationSource:
    slug: str
    nom: str
    actif: bool
    criteres: list[Critere]
    qualifiee: bool
    prochain_releve: datetime | None
    #: Les relevés retenus pour le critère 3, pour le graphique.
    serie_structure: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Qualification:
    sources: list[QualificationSource]
    qualifiees: int
    requises: int
    verdict: str
    contact_renseigne: bool
    referentiel_ingredients: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def structure_stable(comptes: list[int]) -> bool:
    """Règle de `spike01_releve.py --rapport` : un catalogue vit, quelques
    produits entrent et sortent. Ce qu'on cherche, c'est un **effondrement** —
    le signe que le sélecteur ne correspond plus à la page."""
    if not comptes or min(comptes) <= 0:
        return False
    etendue = max(comptes) - min(comptes)
    return etendue <= max(1, round(0.5 * max(comptes)))


def releves_hebdomadaires(releves: list[Releve] | tuple[Releve, ...]) -> list[Releve]:
    """Relevés de structure exploitables, un par fenêtre d'une semaine.

    Parcours du plus ancien au plus récent : un relevé ne compte que s'il
    arrive au moins six jours après le dernier relevé retenu.
    """
    retenus: list[Releve] = []
    for releve in sorted(
        (r for r in releves if r.mode == "structure" and r.exploitable),
        key=lambda r: r.horodatage,
    ):
        if not retenus or releve.horodatage - retenus[-1].horodatage >= ECART_MINIMAL:
            retenus.append(releve)
    return retenus


def critere_1(releves: tuple[Releve, ...]) -> Critere:
    libelle = "Les prix figurent dans le HTML servi"
    if not releves:
        return Critere(1, libelle, INCONNU, "aucun relevé archivé", "Lancer un relevé de structure")
    exploitables = [r for r in releves if r.exploitable]
    if not exploitables:
        return Critere(
            1,
            libelle,
            A_FAIRE,
            f"{len(releves)} relevé(s), aucun prix trouvé",
            "Vérifier le connecteur sur un instantané",
        )
    dernier = max(exploitables, key=lambda r: r.horodatage)
    exemple = f" · ex. {dernier.exemples_prix[0]}" if dernier.exemples_prix else ""
    return Critere(
        1,
        libelle,
        FAIT,
        f"{dernier.prix_trouves} prix trouvés le {dernier.horodatage:%d/%m/%Y}{exemple}",
    )


def critere_2(releves: tuple[Releve, ...], referentiel: int | None) -> Critere:
    libelle = f"Le catalogue couvre au moins {INGREDIENTS_REQUIS} ingrédients du référentiel"
    plafond = (
        f" — plafonné : le référentiel ne compte que {referentiel} ingrédient(s)"
        if referentiel is not None and referentiel < INGREDIENTS_REQUIS
        else ""
    )
    collectes = [r for r in releves if r.mode == "collecte" and r.reussi]
    if not collectes:
        return Critere(
            2,
            libelle,
            INCONNU,
            "aucune collecte à blanc réussie" + plafond,
            "Lancer une collecte à blanc, puis apparier la file de revue",
        )
    derniere = max(collectes, key=lambda r: r.horodatage)
    n = derniere.ingredients_apparies or 0
    etat = FAIT if n >= INGREDIENTS_REQUIS else (PARTIEL if n else A_FAIRE)
    return Critere(
        2,
        libelle,
        etat,
        f"{n}/{INGREDIENTS_REQUIS} ingrédients appariés le {derniere.horodatage:%d/%m/%Y}" + plafond,
        None if etat == FAIT else "Compléter les alias depuis la file de revue (sprint 03 en amont)",
    )


def critere_3(releves: tuple[Releve, ...]) -> tuple[Critere, list[Releve]]:
    libelle = f"La structure est stable sur {RELEVES_REQUIS} relevés espacés d'une semaine"
    retenus = releves_hebdomadaires(releves)
    if len(retenus) < RELEVES_REQUIS:
        return (
            Critere(
                3,
                libelle,
                PARTIEL if retenus else (A_FAIRE if releves else INCONNU),
                f"{len(retenus)}/{RELEVES_REQUIS} relevé(s) hebdomadaire(s) exploitable(s) — non instruit",
                "Relancer un relevé de structure dans une semaine",
            ),
            retenus,
        )

    derniers = retenus[-RELEVES_REQUIS:]
    comptes = [r.prix_trouves for r in derniers]
    stable = structure_stable(comptes)
    return (
        Critere(
            3,
            libelle,
            FAIT if stable else A_FAIRE,
            f"{RELEVES_REQUIS} derniers relevés : {comptes} — "
            + ("structure stable" if stable else "structure instable"),
            None if stable else "Inspecter l'instantané du relevé en baisse",
        ),
        retenus,
    )


def critere_4(etat: EtatSource, contact_renseigne: bool) -> Critere:
    libelle = "Les CGU et le robots.txt n'interdisent pas la collecte"
    avec_verdict = sorted(
        (r for r in etat.releves if r.robots_autorise is not None), key=lambda r: r.horodatage
    )
    manque_contact = "" if contact_renseigne else " · contact du robot non renseigné"

    if not avec_verdict:
        return Critere(4, libelle, INCONNU, "robots.txt jamais lu" + manque_contact, "Lancer un relevé")
    dernier = avec_verdict[-1]
    if dernier.robots_autorise is False:
        return Critere(
            4,
            libelle,
            A_FAIRE,
            f"robots.txt interdit la collecte ({dernier.horodatage:%d/%m/%Y})",
            "Ne pas collecter : la source est à écarter",
        )
    if etat.cgu_attestees_par and etat.cgu_attestees_le:
        return Critere(
            4,
            libelle,
            FAIT if contact_renseigne else PARTIEL,
            f"robots.txt autorise · CGU attestées par {etat.cgu_attestees_par} "
            f"le {etat.cgu_attestees_le:%d/%m/%Y}" + manque_contact,
            None if contact_renseigne else "Renseigner SCRAPING_CONTACT",
        )
    return Critere(
        4,
        libelle,
        PARTIEL,
        "robots.txt autorise · CGU non attestées" + manque_contact,
        "Lire les CGU, puis les attester sur la fiche de la source",
    )


def qualifier(
    sources: list[EtatSource],
    *,
    referentiel_ingredients: int | None,
    contact_renseigne: bool,
) -> Qualification:
    resultats: list[QualificationSource] = []
    for etat in sources:
        c3, retenus = critere_3(etat.releves)
        criteres = [
            critere_1(etat.releves),
            critere_2(etat.releves, referentiel_ingredients),
            c3,
            critere_4(etat, contact_renseigne),
        ]
        resultats.append(
            QualificationSource(
                slug=etat.slug,
                nom=etat.nom,
                actif=etat.actif,
                criteres=criteres,
                qualifiee=all(c.etat == FAIT for c in criteres),
                prochain_releve=(retenus[-1].horodatage + timedelta(days=7)) if retenus else None,
                serie_structure=[
                    {
                        "horodatage": r.horodatage.isoformat(),
                        "prix_trouves": r.prix_trouves,
                        "produits": r.produits,
                        "retenu": r in retenus,
                    }
                    for r in sorted(
                        (r for r in etat.releves if r.mode == "structure"),
                        key=lambda r: r.horodatage,
                    )
                ],
            )
        )

    qualifiees = sum(1 for r in resultats if r.qualifiee)
    verdict = (
        f"{qualifiees} source(s) qualifiée(s) sur {SOURCES_REQUISES} requises — "
        + (
            "condition d'entrée du lot 4 remplie."
            if qualifiees >= SOURCES_REQUISES
            else "le lot 4 reste fermé."
        )
    )
    return Qualification(
        sources=resultats,
        qualifiees=qualifiees,
        requises=SOURCES_REQUISES,
        verdict=verdict,
        contact_renseigne=contact_renseigne,
        referentiel_ingredients=referentiel_ingredients,
    )


# --------------------------------------------------------------------------
# Archive de spike01_releve.py
# --------------------------------------------------------------------------


def lire_archive_spike01(texte: str) -> list[dict[str, Any]]:
    """Lit `spike01/releves.jsonl`. Une ligne illisible est ignorée, pas devinée."""
    lignes: list[dict[str, Any]] = []
    for brute in texte.splitlines():
        if not brute.strip():
            continue
        try:
            ligne = json.loads(brute)
            horodatage = datetime.fromisoformat(ligne["horodatage"])
        except (ValueError, KeyError, TypeError):
            continue
        lignes.append(
            {
                "source": str(ligne.get("source", "")),
                "horodatage": horodatage,
                "http_status": ligne.get("http_status"),
                "robots_autorise": ligne.get("robots_autorise"),
                "produits": int(ligne.get("produits") or 0),
                "prix_trouves": int(ligne.get("prix_trouves") or 0),
                "exemples_prix": list(ligne.get("exemples_prix") or [])[:3],
                "taille_octets": int(ligne.get("taille_octets") or 0),
                "erreur": ligne.get("erreur"),
            }
        )
    return lignes


__all__ = [
    "A_FAIRE",
    "Critere",
    "ECART_MINIMAL",
    "EtatSource",
    "FAIT",
    "INCONNU",
    "INGREDIENTS_REQUIS",
    "PARTIEL",
    "Qualification",
    "QualificationSource",
    "RELEVES_REQUIS",
    "Releve",
    "SOURCES_REQUISES",
    "critere_1",
    "critere_2",
    "critere_3",
    "critere_4",
    "lire_archive_spike01",
    "qualifier",
    "releves_hebdomadaires",
    "structure_stable",
]
