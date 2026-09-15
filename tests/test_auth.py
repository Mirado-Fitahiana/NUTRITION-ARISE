"""Vérification des jetons (D-03, §4.3, §14.5).

Ce que ces tests protègent n'est pas le chemin passant — il est trivial — mais
les **refus**. Un contrôle de sécurité qu'on n'a jamais vu refuser n'est pas un
contrôle vérifié, et le piège n° 4 de la grille d'analyse consiste exactement à
laisser passer un jeton signé avec un secret partagé.

Aucune base n'est nécessaire : la vérification est purement cryptographique.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import Principal, Role, decode_token
from app.core.config import settings
from app.core.errors import ErrorCode, NutritionError


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    """Paire RSA jetable. Ne touche pas `keys/` : les tests ne doivent pas
    dépendre d'un fichier que le développeur peut avoir régénéré."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    directory = tmp_path_factory.mktemp("keys")
    private = directory / "private.pem"
    public = directory / "public.pem"
    private.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private, public


@pytest.fixture(autouse=True)
def use_dev_key(keypair, monkeypatch):
    _, public = keypair
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "jwt_dev_public_key_path", str(public))


def make_token(keypair, **overrides) -> str:
    private, _ = keypair
    now = int(time.time())
    claims = {
        "sub": "user-001",
        "role": "user",
        "entitlements": ["nutrition"],
        "iss": settings.jwt_issuer,
        "aud": [settings.jwt_audience],
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides.pop("claims", {}))
    algorithm = overrides.pop("algorithm", "RS256")
    key = "secret-partage" if algorithm == "HS256" else private.read_text()
    return jwt.encode(claims, key, algorithm=algorithm)


class TestJetonValide:
    async def test_les_claims_sont_reduits_au_necessaire(self, keypair):
        principal = await decode_token(make_token(keypair))
        assert isinstance(principal, Principal)
        assert principal.external_user_id == "user-001"
        assert principal.role == Role.USER
        assert principal.has_entitlement("nutrition")
        assert principal.dev_key is True

    async def test_aucune_donnee_d_identite_n_est_conservee(self, keypair):
        """D-04 — `email` figure dans le jeton ARISE ; il ne doit pas ressortir
        du `Principal`, sans quoi il finirait par être persisté."""
        token = make_token(keypair, claims={"email": "alice@example.org"})
        principal = await decode_token(token)
        assert "email" not in principal.__dict__
        assert "alice@example.org" not in repr(principal)


class TestRefus:
    """Chaque cas correspond à une ligne de §14.5."""

    async def test_hs256_est_refuse(self, keypair):
        """Piège n° 4 : un secret symétrique partagé permettrait à ce service
        de forger des jetons administrateur ARISE."""
        token = make_token(keypair, algorithm="HS256")
        with pytest.raises(NutritionError) as exc:
            await decode_token(token)
        assert exc.value.code is ErrorCode.UNAUTHENTICATED
        assert "HS256" in str(exc.value.details)

    async def test_audience_incorrecte_est_refusee(self, keypair):
        token = make_token(keypair, claims={"aud": ["un-autre-service"]})
        with pytest.raises(NutritionError) as exc:
            await decode_token(token)
        assert exc.value.http_status == 401

    async def test_emetteur_incorrect_est_refuse(self, keypair):
        token = make_token(keypair, claims={"iss": "quelqu-un-d-autre"})
        with pytest.raises(NutritionError):
            await decode_token(token)

    async def test_jeton_expire_est_refuse(self, keypair):
        now = int(time.time())
        token = make_token(keypair, claims={"iat": now - 7200, "exp": now - 60})
        with pytest.raises(NutritionError):
            await decode_token(token)

    async def test_sub_absent_est_refuse(self, keypair):
        """Sans `sub`, il n'y a pas d'`external_user_id` : le profil serait
        rattaché à personne."""
        token = make_token(keypair, claims={"sub": ""})
        with pytest.raises(NutritionError):
            await decode_token(token)

    async def test_jeton_illisible_est_refuse(self):
        with pytest.raises(NutritionError):
            await decode_token("ceci-n-est-pas-un-jeton")

    async def test_le_message_ne_revele_pas_la_cause(self, keypair):
        """Distinguer « signature invalide » de « audience incorrecte » aide
        surtout un attaquant : le motif reste dans `details`, journalisé."""
        token = make_token(keypair, claims={"aud": ["autre"]})
        with pytest.raises(NutritionError) as exc:
            await decode_token(token)
        assert exc.value.message == "Authentification requise."


class TestProductionInterditLesClesLocales:
    def test_settings_refuse_de_demarrer(self, monkeypatch):
        """D-03 — un déploiement mal configuré doit refuser de démarrer, pas
        fonctionner discrètement avec une clé de signature."""
        from pydantic import ValidationError

        from app.core.config import Settings

        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("JWT_DEV_PRIVATE_KEY_PATH", "keys/dev_jwt_private.pem")
        with pytest.raises(ValidationError, match="vides en production"):
            Settings(_env_file=None)
