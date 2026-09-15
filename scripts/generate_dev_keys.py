"""Génère une paire de clés RSA de développement.

    python scripts/generate_dev_keys.py

Elle sert **uniquement** à émettre des jetons de test depuis le banc d'essai,
tant que NestJS n'a pas migré en RS256 et n'expose pas son endpoint JWKS.

D-03 : en production, la clé publique vient de JWKS et le service ne détient
aucune clé privée. `Settings` refuse de démarrer si l'une de ces variables est
renseignée avec `ENVIRONMENT=production`.

`keys/` est ignoré par Git — la clé privée ne doit jamais être versionnée.
"""

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS_DIR = Path(__file__).resolve().parents[1] / "keys"
PRIVATE = KEYS_DIR / "dev_jwt_private.pem"
PUBLIC = KEYS_DIR / "dev_jwt_public.pem"


def _force_utf8_output() -> None:
    """La console Windows est en cp1252 : sans cela, un « ✓ » fait planter
    le script sur un `UnicodeEncodeError` après avoir écrit les clés."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_output()
    KEYS_DIR.mkdir(exist_ok=True)

    if PRIVATE.exists() and "--force" not in sys.argv:
        print(f"✓ paire déjà présente : {PRIVATE.name}, {PUBLIC.name}")
        print("  (relancer avec --force pour la remplacer)")
        return 0

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    PRIVATE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PUBLIC.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    print(f"✓ clés écrites dans {KEYS_DIR}")
    print("\nÀ reporter dans .env :")
    print("  JWT_DEV_PUBLIC_KEY_PATH=keys/dev_jwt_public.pem")
    print("  JWT_DEV_PRIVATE_KEY_PATH=keys/dev_jwt_private.pem")
    print("\nLaisser ces deux variables vides en production.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
