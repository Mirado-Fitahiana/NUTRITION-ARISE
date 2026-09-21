"""Combien de plats avant que pgvector devienne nécessaire ? — sprint 07, §4.

    python scripts/mesurer_similarite.py
    python scripts/mesurer_similarite.py --dimension 1536 --repetitions 5

Le sprint 07 avance que « à quelques centaines de plats, une similarité cosinus
en force brute se compte en millisecondes », et en conclut qu'installer pgvector
maintenant serait de l'optimisation prématurée. Ce script **mesure** cette
affirmation au lieu de la supposer — c'est ce que demande le §4 : *« Mesurer le
temps réel sur le catalogue complet, et le consigner. C'est cette mesure qui
dira, plus tard, quand pgvector devient nécessaire. »*

Deux précautions de lecture :

* les vecteurs sont **synthétiques**. On mesure le coût du calcul, qui ne dépend
  que du nombre de vecteurs et de leur dimension — pas leur pertinence, qui
  dépend du modèle et du catalogue réel ;
* la mesure est faite en **Python pur**, sans `numpy`. C'est la borne
  pessimiste : si elle tient, la dépendance `numpy` prévue par le sprint est
  elle aussi prématurée.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time

#: Dimensions courantes. 1536 = text-embedding-3-small ; 4096 = Qwen3 8B.
DIMENSIONS_COURANTES = (1024, 1536, 4096)

#: Jalons du catalogue. 180 = MVP 7 jours ; 330 = 14 jours ; 450 = 21 jours ;
#: 5 000 = le seuil au-delà duquel le sprint propose de reconsidérer pgvector.
TAILLES = (180, 330, 450, 1000, 5000, 20000)

#: Au-delà, une recherche interactive cesse d'être perçue comme instantanée.
BUDGET_INTERACTIF_MS = 100.0


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def vecteur_normalise(dimension: int, rng: random.Random) -> list[float]:
    valeurs = [rng.gauss(0.0, 1.0) for _ in range(dimension)]
    norme = math.sqrt(sum(v * v for v in valeurs)) or 1.0
    return [v / norme for v in valeurs]


def top_k_cosinus(
    requete: list[float], catalogue: list[list[float]], k: int = 20
) -> list[tuple[float, int]]:
    """Force brute. Les vecteurs étant normalisés, le cosinus se réduit au
    produit scalaire — inutile de recalculer les normes à chaque requête."""
    scores = [
        (sum(a * b for a, b in zip(requete, vecteur, strict=True)), index)
        for index, vecteur in enumerate(catalogue)
    ]
    scores.sort(reverse=True)
    return scores[:k]


def mesurer(taille: int, dimension: int, repetitions: int, rng: random.Random) -> float:
    """Millisecondes médianes pour une recherche top-20 sur `taille` vecteurs."""
    catalogue = [vecteur_normalise(dimension, rng) for _ in range(taille)]
    requete = vecteur_normalise(dimension, rng)

    # Un tour à blanc : le premier appel paie le réchauffage des caches.
    top_k_cosinus(requete, catalogue)

    durees = []
    for _ in range(repetitions):
        debut = time.perf_counter()
        top_k_cosinus(requete, catalogue)
        durees.append((time.perf_counter() - debut) * 1000)
    durees.sort()
    return durees[len(durees) // 2]


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/mesurer_similarite.py",
        description="Mesure la similarité cosinus en force brute (sprint 07, §4).",
    )
    parser.add_argument(
        "--dimension",
        type=int,
        default=1536,
        help="dimension des vecteurs (défaut : 1536, text-embedding-3-small)",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--toutes-dimensions",
        action="store_true",
        help=f"balayer {DIMENSIONS_COURANTES} au lieu d'une seule",
    )
    args = parser.parse_args()

    rng = random.Random(20260915)
    dimensions = DIMENSIONS_COURANTES if args.toutes_dimensions else (args.dimension,)

    print("Similarité cosinus en force brute — Python pur, sans numpy")
    print("Recherche top-20, vecteurs normalisés, médiane sur "
          f"{args.repetitions} répétitions")
    print("=" * 66)

    bascule: dict[int, int | None] = {}

    for dimension in dimensions:
        print()
        print(f"dimension {dimension}")
        print(f"  {'plats':>8}  {'temps':>12}   verdict")
        bascule[dimension] = None
        for taille in TAILLES:
            ms = mesurer(taille, dimension, args.repetitions, rng)
            tenable = ms <= BUDGET_INTERACTIF_MS
            if not tenable and bascule[dimension] is None:
                bascule[dimension] = taille
            verdict = "instantané" if tenable else "perceptible → indexer"
            print(f"  {taille:>8}  {ms:>9.1f} ms   {verdict}")

    print()
    print("=" * 66)
    print(f"Seuil retenu : {BUDGET_INTERACTIF_MS:.0f} ms — au-delà, une recherche")
    print("interactive cesse d'être perçue comme instantanée.")
    print()
    for dimension, seuil in bascule.items():
        if seuil is None:
            print(f"  dimension {dimension} : tenable jusqu'à {TAILLES[-1]} plats — "
                  "pgvector inutile")
        else:
            print(f"  dimension {dimension} : bascule vers {seuil} plats")

    print()
    print("Rappel : le MVP vise ~180 plats publiés (7 jours), ~450 à 21 jours.")
    print("Tant que le catalogue reste dans cet ordre de grandeur, la force brute")
    print("suffit — installer pgvector ou ajouter numpy serait prématuré.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
