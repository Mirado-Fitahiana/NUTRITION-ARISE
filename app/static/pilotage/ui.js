/* Pilotage ARISE — utilitaires d'interface.
 *
 * Règle de sécurité : tout texte venu de l'API passe par `textContent` (via
 * `h()`), jamais par `innerHTML`. Les libellés de produits viennent de sites
 * tiers : un nom piégé ne doit pas pouvoir s'exécuter dans la plateforme.
 */
(function () {
  "use strict";

  const NS_SVG = "http://www.w3.org/2000/svg";

  // --------------------------------------------------------------------------
  // Construction du DOM
  // --------------------------------------------------------------------------

  function ajouter(element, enfants) {
    for (const enfant of enfants.flat(Infinity)) {
      if (enfant === null || enfant === undefined || enfant === false || enfant === "") continue;
      element.appendChild(enfant instanceof Node ? enfant : document.createTextNode(String(enfant)));
    }
    return element;
  }

  function h(balise, attributs) {
    const element = document.createElement(balise);
    for (const [cle, valeur] of Object.entries(attributs || {})) {
      if (valeur === null || valeur === undefined || valeur === false) continue;
      if (cle === "class") element.className = valeur;
      else if (cle === "text") element.textContent = valeur;
      else if (cle === "dataset") Object.assign(element.dataset, valeur);
      else if (cle === "style" && typeof valeur === "object") Object.assign(element.style, valeur);
      else if (cle.startsWith("on") && typeof valeur === "function") element.addEventListener(cle.slice(2), valeur);
      else if (cle === "value") element.value = valeur;
      else if (valeur === true) element.setAttribute(cle, "");
      else element.setAttribute(cle, String(valeur));
    }
    return ajouter(element, Array.prototype.slice.call(arguments, 2));
  }

  function remplacer(element) {
    element.replaceChildren();
    return ajouter(element, Array.prototype.slice.call(arguments, 1));
  }

  function $(selecteur, racine) {
    return (racine || document).querySelector(selecteur);
  }

  function icone(nom, classe) {
    const svg = document.createElementNS(NS_SVG, "svg");
    svg.setAttribute("class", "icone" + (classe ? " " + classe : ""));
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(NS_SVG, "use");
    use.setAttribute("href", "#i-" + nom);
    svg.appendChild(use);
    return svg;
  }

  function code(texte) {
    return h("code", null, texte);
  }

  // --------------------------------------------------------------------------
  // Formats (fr-FR)
  // --------------------------------------------------------------------------

  function nombre(valeur, decimales) {
    if (valeur === null || valeur === undefined || valeur === "" || Number.isNaN(Number(valeur))) return "—";
    return new Intl.NumberFormat("fr-FR", { maximumFractionDigits: decimales || 0 }).format(Number(valeur));
  }

  function pourcentage(ratio, decimales) {
    if (ratio === null || ratio === undefined || Number.isNaN(Number(ratio))) return "—";
    return new Intl.NumberFormat("fr-FR", { style: "percent", maximumFractionDigits: decimales || 0 }).format(Number(ratio));
  }

  function ariary(valeur) {
    return valeur === null || valeur === undefined ? "—" : nombre(valeur) + " Ar";
  }

  function date(iso, avecHeure) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const options = { day: "2-digit", month: "2-digit", year: "numeric" };
    if (avecHeure !== false) Object.assign(options, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleString("fr-FR", options);
  }

  function heure(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function duree(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return nombre(ms) + " ms";
    const secondes = Math.round(ms / 1000);
    if (secondes < 60) return secondes + " s";
    return Math.floor(secondes / 60) + " min " + String(secondes % 60).padStart(2, "0") + " s";
  }

  function octets(n) {
    if (n === null || n === undefined) return "—";
    if (n < 1024) return nombre(n) + " o";
    if (n < 1048576) return nombre(n / 1024, 1) + " Ko";
    return nombre(n / 1048576, 1) + " Mo";
  }

  // --------------------------------------------------------------------------
  // États et statuts
  // --------------------------------------------------------------------------

  const ETATS = {
    ok: ["OK", "ok"],
    alerte: ["Alerte", "alerte"],
    critique: ["Critique", "critique"],
    inconnu: ["Inconnu", "inconnu"],
    fait: ["Fait", "ok"],
    partiel: ["Partiel", "alerte"],
    afaire: ["À faire", "critique"],
  };

  const STATUTS = {
    pending: ["En attente", "info"],
    running: ["En cours", "encours"],
    succeeded: ["Réussie", "ok"],
    failed: ["Échouée", "critique"],
    cancelled: ["Annulée", "inconnu"],
    interrupted: ["Interrompue", "alerte"],
    generating: ["En génération", "encours"],
    active: ["Actif", "ok"],
  };

  function badge(etat, texte) {
    const [libelle, classe] = ETATS[etat] || [etat, etat || "inconnu"];
    return h("span", { class: "badge badge--" + classe }, texte || libelle);
  }

  function badgeStatut(statut) {
    const [libelle, classe] = STATUTS[statut] || [statut, "inconnu"];
    return h("span", { class: "badge badge--" + classe }, libelle);
  }

  // --------------------------------------------------------------------------
  // Retours visuels
  // --------------------------------------------------------------------------

  function toast(message, type, delai) {
    const zone = document.getElementById("toasts");
    if (!zone) return;
    const noms = { succes: "check", erreur: "error", alerte: "warning", info: "info" };
    const element = h("div", { class: "toast toast--" + (type || "info"), role: "status" }, icone(noms[type] || "info"), h("span", null, message));
    zone.appendChild(element);
    setTimeout(() => element.remove(), delai || 5000);
  }

  function toastApresNavigation(message, type) {
    try {
      sessionStorage.setItem("arise.pilotage.toast", JSON.stringify({ message: message, type: type }));
    } catch (e) {
      /* rien */
    }
  }

  function messageErreur(erreur) {
    if (!erreur) return "Erreur inconnue.";
    if (erreur.name === "ErreurApi") {
      if (erreur.status === 0) return "Service Nutrition injoignable : est-il démarré (python run.py) ?";
      return erreur.message;
    }
    return erreur.message || String(erreur);
  }

  function bandeau(type, titre, corps, meta) {
    const noms = { erreur: "error", alerte: "warning", succes: "check", info: "info", fictif: "laboratoire" };
    return h(
      "div",
      { class: "bandeau bandeau--" + type, role: type === "erreur" ? "alert" : null },
      icone(noms[type] || "info"),
      h("div", { class: "bandeau__texte" }, titre && h("p", { class: "bandeau__titre" }, titre), corps && h("div", null, corps), meta && h("p", { class: "bandeau__meta" }, meta))
    );
  }

  function blocErreur(erreur) {
    const conseils = [];
    if (erreur && erreur.status === 503) {
      conseils.push(h("p", null, "Vérifier ", code("DATABASE_*"), " dans ", code(".env"), ", puis appliquer ", code("alembic upgrade head"), "."));
    }
    if (erreur && erreur.url && erreur.url.indexOf("dev-token") !== -1) {
      conseils.push(h("p", null, "Générer les clés de développement : ", code("python scripts/generate_dev_keys.py"), "."));
    }
    const meta = erreur && erreur.status ? ["HTTP " + erreur.status, erreur.code, erreur.requestId && "requestId " + erreur.requestId].filter(Boolean).join(" · ") : null;
    return bandeau("erreur", messageErreur(erreur), conseils, meta);
  }

  function chargement(lignes) {
    const bloc = h("div", { class: "squelette", "aria-busy": "true", "aria-label": "Chargement" });
    for (let i = 0; i < (lignes || 3); i += 1) bloc.appendChild(h("span"));
    return bloc;
  }

  function vide(titre, texte, nomIcone) {
    return h("div", { class: "vide" }, icone(nomIcone || "info"), h("p", { class: "vide__titre" }, titre), texte && h("p", null, texte));
  }

  function occuper(bouton, occupe) {
    if (!bouton) return;
    bouton.classList.toggle("est-occupe", occupe);
    bouton.disabled = occupe;
  }

  // --------------------------------------------------------------------------
  // Composants
  // --------------------------------------------------------------------------

  function carteStat(titre, valeur, etat, sous) {
    return h("article", { class: "carte-stat carte-stat--" + (etat || "aubergine") }, h("p", { class: "carte-stat__valeur" }, valeur), h("p", { class: "carte-stat__titre" }, titre), sous && h("p", { class: "carte-stat__sous" }, sous));
  }

  /* `<label>` seulement autour d'un contrôle unique : un label qui englobe un
     groupe de cases transmet le clic sur son titre à la première case. */
  function champ(libelle, saisie, aide) {
    const controleUnique = saisie instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(saisie.tagName);
    return h(
      controleUnique ? "label" : "div",
      { class: "champ", role: controleUnique ? null : "group" },
      h("span", { class: "champ__libelle" }, libelle),
      saisie,
      aide && h("span", { class: "champ__aide" }, aide)
    );
  }

  function lien(url, texte) {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return h("span", null, texte || "—");
    return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "lien-externe", title: url }, texte || url, icone("open", "icone--xs"));
  }

  /* `lignes` : [{ cellules: [...], lien?: url, classe?: str }]. Les cellules sont
     du texte ou des nœuds ; `numeriques` liste les colonnes alignées à droite. */
  function tableau(entetes, lignes, options) {
    const numeriques = new Set((options && options.numeriques) || []);
    const corps = h("tbody");
    for (const ligne of lignes) {
      const tr = h("tr", { class: [ligne.lien || ligne.action ? "est-cliquable" : "", ligne.classe || ""].join(" ").trim() || null });
      ligne.cellules.forEach((cellule, i) => tr.appendChild(h("td", { class: numeriques.has(i) ? "num" : null }, cellule)));
      if (ligne.lien || ligne.action) {
        tr.tabIndex = 0;
        const agir = () => (ligne.action ? ligne.action() : (location.href = ligne.lien));
        tr.addEventListener("click", (evenement) => {
          if (evenement.target.closest("a, button, select, input")) return;
          agir();
        });
        tr.addEventListener("keydown", (evenement) => {
          if (evenement.key === "Enter") agir();
        });
      }
      corps.appendChild(tr);
    }
    return h("div", { class: "defilement" }, h("table", { class: "tableau" }, h("thead", null, h("tr", null, entetes.map((e, i) => h("th", { class: numeriques.has(i) ? "num" : null }, e)))), corps));
  }

  function onglets(racine, auChangement) {
    const boutons = Array.from(racine.querySelectorAll("[data-onglet]"));
    const panneaux = Array.from(document.querySelectorAll("[data-panneau]"));
    function activer(nom, memoriser) {
      boutons.forEach((b) => {
        const actif = b.dataset.onglet === nom;
        b.classList.toggle("est-actif", actif);
        b.setAttribute("aria-selected", String(actif));
      });
      panneaux.forEach((p) => (p.hidden = p.dataset.panneau !== nom));
      if (memoriser) history.replaceState(null, "", "#" + nom);
      if (auChangement) auChangement(nom);
    }
    boutons.forEach((b) => b.addEventListener("click", () => activer(b.dataset.onglet, true)));
    const demande = location.hash.slice(1);
    activer(boutons.some((b) => b.dataset.onglet === demande) ? demande : boutons[0].dataset.onglet, false);
    return activer;
  }

  function segmente(racine, auChangement) {
    const options = Array.from(racine.querySelectorAll(".segmente__option"));
    options.forEach((option) =>
      option.addEventListener("click", () => {
        options.forEach((o) => o.classList.toggle("est-actif", o === option));
        auChangement(option.dataset.valeur, option);
      })
    );
  }

  function dialogue(titre, corps, actions, large) {
    const modale = document.getElementById("modale");
    modale.classList.toggle("modale--large", Boolean(large));
    remplacer(modale, h("form", { method: "dialog", class: "modale__boite" }, h("h2", { class: "modale__titre" }, titre), h("div", { class: "modale__corps" }, corps), h("div", { class: "modale__actions" }, actions)));
    return modale;
  }

  function confirmer(options) {
    return new Promise((resoudre) => {
      const modale = dialogue(options.titre, options.corps, [
        h("button", { class: "bouton bouton--fantome", value: "non" }, "Annuler"),
        h("button", { class: "bouton " + (options.danger ? "bouton--danger" : "bouton--primaire"), value: "oui" }, options.libelle || "Confirmer"),
      ]);
      modale.addEventListener("close", () => resoudre(modale.returnValue === "oui"), { once: true });
      modale.returnValue = "";
      modale.showModal();
    });
  }

  function panneau(titre, corps) {
    const modale = dialogue(titre, corps, [h("button", { class: "bouton bouton--contour", value: "fermer" }, "Fermer")], true);
    modale.showModal();
  }

  async function copier(texte) {
    try {
      await navigator.clipboard.writeText(texte);
      toast("Copié dans le presse-papiers.", "succes", 2500);
    } catch (e) {
      toast("Copie impossible depuis ce navigateur.", "erreur");
    }
  }

  // --------------------------------------------------------------------------
  // En-tête : état du service, mesuré
  // --------------------------------------------------------------------------

  async function etatsService() {
    const zone = document.getElementById("etats-service");
    if (!zone) return;
    const puceBase = h("span", { class: "puce puce--attente" }, icone("base"), "Base…");
    const puceJeton = h("span", { class: "puce puce--attente" }, icone("shield"), "Jeton…");
    remplacer(zone, puceBase, puceJeton);

    try {
      const reponse = await fetch("/ready", { headers: { "X-Request-ID": "pilotage-ready" } });
      const donnees = await reponse.json();
      const base = (donnees.checks && donnees.checks.database) || {};
      if (base.status === "up") {
        puceBase.className = "puce puce--ok";
        remplacer(puceBase, icone("base"), "Base · " + (base.migration || "?"));
        puceBase.title = "Base joignable — révision Alembic " + base.migration;
      } else {
        puceBase.className = "puce puce--critique";
        remplacer(puceBase, icone("base"), "Base injoignable");
        puceBase.title = base.reason || base.status || "";
      }
    } catch (e) {
      puceBase.className = "puce puce--critique";
      remplacer(puceBase, icone("base"), "Service injoignable");
    }

    try {
      const jeton = await Api.jeton();
      puceJeton.className = "puce puce--ok";
      remplacer(puceJeton, icone("shield"), (jeton.role || "admin") + " · jeton de dev");
      puceJeton.title = "Jeton de développement (sub " + jeton.sub + ") — refusé en production";
    } catch (erreur) {
      puceJeton.className = "puce puce--critique";
      remplacer(puceJeton, icone("shield"), "Jeton indisponible");
      puceJeton.title = messageErreur(erreur);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    etatsService();
    try {
      const differe = sessionStorage.getItem("arise.pilotage.toast");
      if (differe) {
        sessionStorage.removeItem("arise.pilotage.toast");
        const { message, type } = JSON.parse(differe);
        toast(message, type, 9000);
      }
    } catch (e) {
      /* rien */
    }
  });

  window.UI = {
    $: $,
    h: h,
    remplacer: remplacer,
    icone: icone,
    code: code,
    nombre: nombre,
    pourcentage: pourcentage,
    ariary: ariary,
    date: date,
    heure: heure,
    duree: duree,
    octets: octets,
    badge: badge,
    badgeStatut: badgeStatut,
    toast: toast,
    toastApresNavigation: toastApresNavigation,
    messageErreur: messageErreur,
    bandeau: bandeau,
    blocErreur: blocErreur,
    chargement: chargement,
    vide: vide,
    occuper: occuper,
    carteStat: carteStat,
    champ: champ,
    lien: lien,
    tableau: tableau,
    onglets: onglets,
    segmente: segmente,
    confirmer: confirmer,
    panneau: panneau,
    copier: copier,
  };
})();
