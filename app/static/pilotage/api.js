/* Pilotage ARISE — client des API d'administration.
 *
 * Toutes les pages passent par ici. En développement, le jeton vient du banc
 * d'essai (`POST /console/api/auth/dev-token`, rôle admin) et vit dans
 * `sessionStorage` : il disparaît avec l'onglet. Ce mécanisme est refusé par le
 * service en production — la plateforme n'y existe d'ailleurs pas.
 *
 * Chaque requête porte un `X-Request-ID` (FN-036) ; une erreur garde l'enveloppe
 * du service (code stable, message, requestId) pour être affichée telle quelle.
 */
(function () {
  "use strict";

  const CLE_JETON = "arise.pilotage.jeton";
  const SUJET = "pilotage-admin";

  let jeton = null;
  let enCours = null;

  class ErreurApi extends Error {
    constructor(status, corps, url) {
      super((corps && corps.message) || (status ? `HTTP ${status}` : "Service injoignable"));
      this.name = "ErreurApi";
      this.status = status;
      this.corps = corps || {};
      this.code = this.corps.code || null;
      this.requestId = this.corps.requestId || null;
      this.url = url;
    }
  }

  function identifiant() {
    return "pilotage-" + Math.random().toString(36).slice(2, 10);
  }

  function lireStocke() {
    try {
      const brut = sessionStorage.getItem(CLE_JETON);
      if (!brut) return null;
      const stocke = JSON.parse(brut);
      // Une minute de marge : un jeton qui expire pendant la requête vaut un jeton expiré.
      if (!stocke.valeur || (stocke.expire_a && stocke.expire_a * 1000 < Date.now() + 60000)) {
        return null;
      }
      return stocke;
    } catch (e) {
      return null;
    }
  }

  function stocker(valeur) {
    try {
      sessionStorage.setItem(CLE_JETON, JSON.stringify(valeur));
    } catch (e) {
      /* stockage indisponible : le jeton reste en mémoire */
    }
  }

  async function lireCorps(reponse) {
    const texte = await reponse.text();
    if (!texte) return null;
    try {
      return JSON.parse(texte);
    } catch (e) {
      return { message: texte.slice(0, 300) };
    }
  }

  async function obtenirJeton(renouveler) {
    if (!renouveler) {
      jeton = jeton || lireStocke();
      if (jeton) return jeton;
    }
    if (enCours) return enCours;

    enCours = (async function () {
      const url = "/console/api/auth/dev-token";
      let reponse;
      try {
        reponse = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Request-ID": identifiant() },
          body: JSON.stringify({ sub: SUJET, role: "admin", entitlements: ["nutrition"] }),
        });
      } catch (e) {
        throw new ErreurApi(0, { message: "Service Nutrition injoignable." }, url);
      }
      const corps = await lireCorps(reponse);
      if (!reponse.ok) throw new ErreurApi(reponse.status, corps, url);
      jeton = {
        valeur: corps.token,
        expire_a: corps.claims && corps.claims.exp,
        role: corps.claims && corps.claims.role,
        sub: corps.claims && corps.claims.sub,
      };
      stocker(jeton);
      return jeton;
    })();

    try {
      return await enCours;
    } finally {
      enCours = null;
    }
  }

  async function requete(methode, url, corps, options) {
    const envoyer = async function (valeur) {
      try {
        return await fetch(url, {
          method: methode,
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer " + valeur,
            "X-Request-ID": identifiant(),
          },
          body: corps === undefined ? undefined : JSON.stringify(corps),
          signal: options && options.signal,
        });
      } catch (e) {
        if (e && e.name === "AbortError") throw e;
        throw new ErreurApi(0, { message: "Service Nutrition injoignable." }, url);
      }
    };

    let reponse = await envoyer((await obtenirJeton(false)).valeur);
    if (reponse.status === 401) {
      // Jeton expiré ou clés de développement régénérées : un seul renouvellement.
      reponse = await envoyer((await obtenirJeton(true)).valeur);
    }
    const donnees = await lireCorps(reponse);
    if (!reponse.ok) throw new ErreurApi(reponse.status, donnees, url);
    return donnees;
  }

  window.Api = {
    ErreurApi: ErreurApi,
    get: (url, options) => requete("GET", url, undefined, options),
    post: (url, corps, options) => requete("POST", url, corps === undefined ? {} : corps, options),
    put: (url, corps, options) => requete("PUT", url, corps, options),
    patch: (url, corps, options) => requete("PATCH", url, corps, options),
    supprimer: (url, options) => requete("DELETE", url, undefined, options),
    jeton: () => obtenirJeton(false),
  };
})();
