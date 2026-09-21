"""Collecte de prix — lot 4, conditionnel à SPIKE-01 (FN-013, plan §5).

* `politesse` — `robots.txt`, User-Agent, délai plancher, périmètre du site ;
* `extraction` — lecture prudente des prix et des conditionnements ;
* `connecteurs` — un connecteur par plateforme (PrestaShop, WooCommerce) ;
* `moteur` — le déroulé d'une collecte, sans base ni réseau imposés ;
* `qualification` — les quatre critères de SPIKE-01, mesurés ;
* `lancement` — le pont avec la base et les tâches de fond.

Aucun de ces modules n'écrit dans `ingredient_prices` : les offres extraites
restent en transit (`scraped_offers`) tant que le lot 4 n'est pas ouvert.
"""
