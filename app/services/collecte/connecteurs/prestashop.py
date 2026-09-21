"""Connecteur PrestaShop 1.7+ — `kibo.mg`.

Sélecteurs du thème de référence (`classic`), ceux que le relevé SPIKE-01 a
confirmés sur la page servie : chaque produit est un bloc
`.js-product-miniature`, son prix un `span.price`.

`span.price` et non `.price` tout court : le prix barré d'une promotion porte la
classe `regular-price`, et le confondre avec le prix courant ferait remonter
l'ancien tarif.
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


class ConnecteurPrestaShop:
    plateforme = "prestashop"

    def extraire(self, html: str, url_page: str) -> ResultatExtraction:
        soupe = BeautifulSoup(html, "html.parser")
        blocs = soupe.select(".js-product-miniature, article.product-miniature")

        offres: list[OffreBrute] = []
        for bloc in blocs:
            titre = bloc.select_one(".product-title a, .product-title, h2 a, h3 a")
            libelle = texte(titre)
            if not libelle:
                continue

            lien = titre if titre is not None and titre.name == "a" else bloc.select_one("a[href]")
            prix = bloc.select_one(".product-price-and-shipping span.price, span.price")
            disponibilite = bloc.select_one(".product-unavailable, .product-availability")

            offres.append(
                OffreBrute(
                    libelle=libelle,
                    url=lien_absolu(lien.get("href") if lien is not None else None, url_page),
                    prix_texte=texte(prix) or None if prix is not None else None,
                    disponibilite=texte(disponibilite) or None,
                )
            )

        return ResultatExtraction(offres=tuple(offres), blocs_produit=len(blocs))


enregistrer(ConnecteurPrestaShop())
