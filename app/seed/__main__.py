"""Application du seed : `python -m app.seed` (FN-040).

    python -m app.seed --dry-run     # valide les YAML sans rien écrire
    python -m app.seed               # applique le seed (idempotent)
    python -m app.seed --path seeds  # autre répertoire de données

`--dry-run` est la commande à exécuter en intégration continue et avant chaque
revue : elle valide le format, résout les références croisées, calcule les
valeurs nutritionnelles et signale les publications qui seraient refusées —
sans toucher la base.
"""

import argparse
import sys
from pathlib import Path

from app.core.database import create_sync_session_factory
from app.seed.loader import run_seed

DEFAULT_SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"


def _force_utf8_output() -> None:
    """La console Windows est en cp1252 : sans cela, un accent ou un « ✓ » fait
    planter le script sur un `UnicodeEncodeError`, ce qui masquerait le rapport."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Charge le catalogue d'ingrédients et de plats (idempotent).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_SEEDS_DIR,
        help=f"répertoire des fichiers de seed (défaut : {DEFAULT_SEEDS_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="valide et calcule sans écrire en base",
    )
    args = parser.parse_args()

    if not args.path.exists():
        print(f"✗ répertoire introuvable : {args.path}", file=sys.stderr)
        return 2

    mode = "VALIDATION (aucune écriture)" if args.dry_run else "APPLICATION"
    print(f"Seed catalogue ARISE — {mode}")
    print(f"Source : {args.path}")

    if args.dry_run:
        report = run_seed(session=None, seeds_dir=args.path, dry_run=True)
    else:
        session_factory = create_sync_session_factory()
        with session_factory() as session:
            report = run_seed(session, args.path, dry_run=False)

    print(report.render())

    if not report.ok:
        print("\n✗ seed interrompu — aucune modification appliquée", file=sys.stderr)
        return 1

    print("\n✓ seed terminé")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
