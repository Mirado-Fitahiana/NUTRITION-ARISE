"""Un connecteur par plateforme de boutique (FN-013 : « un connecteur isolé par source »).

Deux sources pré-qualifiées reposent sur deux plateformes courantes ; le
connecteur porte les sélecteurs de la plateforme, la source porte ses pages.
Ajouter une source sur une plateforme connue ne demande donc aucun code — mais
ajouter une plateforme passe par une revue et un test sur instantané.
"""

from app.services.collecte.connecteurs.base import (
    Connecteur,
    ResultatExtraction,
    connecteur_pour,
    plateformes,
)

__all__ = ["Connecteur", "ResultatExtraction", "connecteur_pour", "plateformes"]
