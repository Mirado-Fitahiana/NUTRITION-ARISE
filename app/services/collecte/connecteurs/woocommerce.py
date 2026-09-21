"""Connecteur WooCommerce — `abcie.org`.

Chaque produit est un `li.product` ; son prix, un ou plusieurs
`.woocommerce-Price-amount` dans `.price`. Trois cas se présentent :

* **promotion** — l'ancien prix est dans `<del>`, le prix courant dans `<ins>` :
  seul `<ins>` compte ;
* **produit variable** — deux montants (« 8 400 Ar – 23 400 Ar ») : c'est une
  fourchette, rendue telle quelle pour que la lecture la refuse comme ambiguë ;
* **aucun montant** (« Prix sur demande ») — le texte est rendu tel quel : la
  lecture le classera illisible, c'est bien un prix affiché qu'on ne sait pas lire.

Le relevé SPIKE-01 du 15/09 a compté 21 produits pour 12 prix sur `/boutique/` :
les neuf autres n'affichent pas de prix, et ce n'est pas un échec d'extraction.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from app.services.collecte.connecteurs.base import (
    ResultatExtraction,
    enregistrer,
    lien_absolu,
    texte,
)
from app.services.collecte.extraction import OffreBrute


class ConnecteurWooCommerce:
    plateforme = "woocommerce"

    def extraire(self, html: str, url_page: str) -> ResultatExtraction:
        soupe = BeautifulSoup(html, "html.parser")
        blocs = soupe.select("li.product, div.product.type-product")

        offres: list[OffreBrute] = []
        for bloc in blocs:
            titre = bloc.select_one(".woocommerce-loop-product__title, h2, h3")
            libelle = texte(titre)
            if not libelle:
                continue

            lien = bloc.select_one("a.woocommerce-LoopProduct-link, a[href]")
            classes = bloc.get("class") or []

            offres.append(
                OffreBrute(
                    libelle=libelle,
                    url=lien_absolu(lien.get("href") if lien is not None else None, url_page),
                    prix_texte=self._prix(bloc),
                    disponibilite="rupture" if "outofstock" in classes else None,
                )
            )

        return ResultatExtraction(offres=tuple(offres), blocs_produit=len(blocs))

    @staticmethod
    def _prix(bloc) -> str | None:
        zone = bloc.select_one(".price")
        if zone is None:
            return None

        courant = zone.select_one("ins .woocommerce-Price-amount")
        if courant is not None:
            return texte(courant) or None

        montants = zone.select(".woocommerce-Price-amount")
        if len(montants) == 1:
            return texte(montants[0]) or None
        if len(montants) > 1:
            return " – ".join(texte(m) for m in montants)
        return texte(zone) or None


enregistrer(ConnecteurWooCommerce())
