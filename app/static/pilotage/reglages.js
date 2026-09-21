/* Réglages — paramètres métier, poids du score, environnement (plan §7).
 * La valeur affichée est la valeur effective : celle que le moteur utilise. */
(function () {
  "use strict";

  const { h, icone, remplacer } = UI;
  const API = "/api/v1/admin";
  const zones = {
    parametres: document.getElementById("parametres"),
    poids: document.getElementById("poids"),
    environnement: document.getElementById("environnement"),
  };
  const CRENEAUX = ["breakfast", "morning_snack", "lunch", "afternoon_snack", "dinner"];
  const LIBELLES_CRENEAUX = {
    breakfast: "Petit-déjeuner",
    morning_snack: "Collation du matin",
    lunch: "Déjeuner",
    afternoon_snack: "Collation de l'après-midi",
    dinner: "Dîner",
  };
  const SOURCES = { base: ["ok", "En base"], defaut: ["inconnu", "Défaut"], rejete: ["critique", "Rejeté"] };
  const charges = {};

  // --------------------------------------------------------------------------
  // Paramètres métier
  // --------------------------------------------------------------------------

  function afficher(r, valeur) {
    if (valeur === null || valeur === undefined) return "—";
    if (r.type === "booleen") return valeur ? "activé" : "désactivé";
    if (r.type === "repartition") return CRENEAUX.map((c) => `${LIBELLES_CRENEAUX[c]} ${UI.pourcentage(Number(valeur[c] || 0))}`).join(" · ");
    if (r.unite === "ratio") return `${valeur} (${UI.pourcentage(Number(valeur), 1)})`;
    return `${valeur}${r.unite ? " " + r.unite : ""}`;
  }

  function ligne(r, baseJoignable) {
    const verrouille = !r.branche;
    const modifiable = !verrouille && baseJoignable;
    const enregistrer = h("button", { type: "button", class: "bouton bouton--primaire bouton--petit", disabled: true }, "Enregistrer");
    const marquer = () => (enregistrer.disabled = !modifiable);
    let controle;
    let lire;

    if (r.type === "booleen") {
      const bascule = h("input", { type: "checkbox", class: "interrupteur", disabled: !modifiable });
      bascule.checked = r.effectif === true;
      bascule.addEventListener("change", marquer);
      controle = h("label", { class: "interrupteur-champ" }, bascule, "Activé");
      lire = () => bascule.checked;
    } else if (r.type === "repartition") {
      const saisies = {};
      const somme = h("strong");
      const barre = h("div", { class: "progression__barre" });
      const mettreAJour = () => {
        const total = CRENEAUX.reduce((t, c) => t + (Number(saisies[c].value) || 0), 0);
        somme.textContent = `Somme : ${UI.nombre(total, 1)} % ${Math.abs(total - 100) < 0.1 ? "✓" : "— elle doit valoir 100 %"}`;
        barre.style.width = `${Math.min(100, total)}%`;
        barre.style.background = Math.abs(total - 100) < 0.1 ? "var(--succes)" : "var(--alerte)";
      };
      const lignes = CRENEAUX.map((c) => {
        const saisie = h("input", { type: "number", class: "champ__saisie", min: 0, max: 100, step: 0.5, disabled: !modifiable, value: (Number((r.effectif || {})[c] || 0) * 100).toFixed(1), "aria-label": LIBELLES_CRENEAUX[c] });
        saisie.addEventListener("input", () => {
          mettreAJour();
          marquer();
        });
        saisies[c] = saisie;
        return [h("span", null, LIBELLES_CRENEAUX[c]), saisie];
      });
      controle = h("div", { class: "pile" }, h("div", { class: "repartition-saisie" }, lignes), h("div", { class: "progression" }, barre), somme);
      mettreAJour();
      lire = () => Object.fromEntries(CRENEAUX.filter((c) => Number(saisies[c].value) > 0).map((c) => [c, (Number(saisies[c].value) / 100).toFixed(4)]));
    } else {
      const saisie = h("input", {
        type: "number",
        class: "champ__saisie",
        min: r.minimum,
        max: r.maximum,
        step: r.type === "entier" ? 1 : 0.01,
        disabled: !modifiable,
        value: r.effectif === null || r.effectif === undefined ? "" : r.effectif,
        "aria-label": r.libelle,
      });
      saisie.addEventListener("input", marquer);
      controle = h("div", { class: "rangee", style: { flexWrap: "nowrap" } }, saisie, r.unite && h("span", { class: "aide" }, r.unite));
      lire = () => saisie.value;
    }

    enregistrer.addEventListener("click", async () => {
      UI.occuper(enregistrer, true);
      try {
        await Api.put(`${API}/settings/${encodeURIComponent(r.cle)}`, { value: lire() });
        UI.toast(`« ${r.libelle} » enregistré.`, "succes");
        chargerParametres();
      } catch (erreur) {
        UI.toast(UI.messageErreur(erreur), "erreur", 9000);
        UI.occuper(enregistrer, false);
      }
    });

    const reinitialiser =
      r.present &&
      baseJoignable &&
      h(
        "button",
        {
          type: "button",
          class: "bouton bouton--fantome bouton--petit",
          onclick: async (evenement) => {
            const bouton = evenement.currentTarget;
            UI.occuper(bouton, true);
            try {
              await Api.supprimer(`${API}/settings/${encodeURIComponent(r.cle)}`);
              UI.toast(`« ${r.libelle} » revient à sa valeur par défaut.`, "succes");
              chargerParametres();
            } catch (erreur) {
              UI.toast(UI.messageErreur(erreur), "erreur", 8000);
              UI.occuper(bouton, false);
            }
          },
        },
        "Revenir au défaut"
      );

    const [classeSource, libelleSource] = SOURCES[r.source] || ["inconnu", r.source];
    return h(
      "article",
      { class: "reglage" + (verrouille ? " est-verrouille" : "") },
      h(
        "div",
        null,
        h(
          "div",
          { class: "reglage__tete" },
          h("h3", { class: "reglage__libelle" }, r.libelle),
          h("span", { class: "badge badge--info" }, r.reference),
          verrouille ? h("span", { class: "badge badge--inconnu" }, icone("cadenas", "icone--xs"), "Non branché") : UI.badge(classeSource, libelleSource)
        ),
        h("p", { class: "aide", style: { marginTop: "6px" } }, r.description),
        h(
          "div",
          { class: "reglage__meta" },
          h("span", null, "Lu par ", UI.code(r.lu_par)),
          h("span", null, `Effectif : ${afficher(r, r.effectif)}`),
          h("span", null, `Défaut : ${afficher(r, r.defaut)}`),
          (r.minimum !== null || r.maximum !== null) && h("span", null, `Bornes : ${r.minimum === null ? "—" : r.minimum} à ${r.maximum === null ? "—" : r.maximum}`),
          r.mis_a_jour_le && h("span", null, `Modifié le ${UI.date(r.mis_a_jour_le)} par ${r.mis_a_jour_par || "—"}`)
        ),
        r.source === "rejete" && h("div", { style: { marginTop: "12px" } }, UI.bandeau("erreur", "Valeur stockée rejetée — le défaut s'applique", [h("p", null, r.motif), h("p", null, "En base : ", UI.code(JSON.stringify(r.stocke)))])),
        verrouille && h("p", { class: "aide", style: { marginTop: "8px" } }, icone("cadenas", "icone--xs"), " Aucun traitement ne lit ce réglage : l'enregistrer n'aurait aucun effet, l'API le refuse.")
      ),
      h("div", { class: "reglage__controle" }, controle, h("div", { class: "rangee rangee--fin" }, reinitialiser, !verrouille && enregistrer))
    );
  }

  async function chargerParametres() {
    remplacer(zones.parametres, h("div", { class: "carte" }, UI.chargement(8)));
    try {
      const donnees = await Api.get(`${API}/settings`);
      const blocs = [];
      if (!donnees.base_joignable) {
        blocs.push(UI.bandeau("alerte", "Base injoignable — lecture seule", [h("p", null, "Le registre affiche les valeurs par défaut ; l'enregistrement exige la base."), donnees.motif && h("p", null, donnees.motif)]));
      }
      if (donnees.cles_inconnues.length) {
        blocs.push(UI.bandeau("alerte", "Clés inconnues en base, sans effet", `app_settings contient ${donnees.cles_inconnues.join(", ")} : aucun traitement ne les lit.`));
      }
      donnees.groupes.forEach((groupe) => {
        blocs.push(
          h(
            "section",
            { class: "carte" },
            h("header", { class: "carte__tete" }, h("h2", { class: "carte__titre" }, groupe)),
            donnees.reglages.filter((r) => r.groupe === groupe).map((r) => ligne(r, donnees.base_joignable))
          )
        );
      });
      remplacer(zones.parametres, blocs);
    } catch (erreur) {
      remplacer(zones.parametres, UI.blocErreur(erreur));
    }
  }

  // --------------------------------------------------------------------------
  // Poids du score
  // --------------------------------------------------------------------------

  function nombres(poids) {
    return Object.fromEntries(Object.entries(poids || {}).map(([cle, valeur]) => [cle, Number(valeur)]));
  }

  function somme(poids) {
    return Object.values(nombres(poids)).reduce((t, v) => t + (Number.isFinite(v) ? v : 0), 0);
  }

  async function chargerPoids() {
    remplacer(zones.poids, h("div", { class: "carte" }, UI.chargement(8)));
    try {
      const donnees = await Api.get(`${API}/scoring-weights`);
      const libelles = Object.fromEntries(donnees.autorises.map((a) => [a.cle, a.libelle]));
      const actif = donnees.jeux.find((j) => j.is_active);
      const valeursActives = actif ? actif.effectifs : donnees.repli.weights;
      const plafond = Math.max(somme(donnees.repli.weights), ...donnees.jeux.map((j) => somme(j.effectifs)));
      const blocs = [];

      if (!donnees.base_joignable) blocs.push(UI.bandeau("alerte", "Base injoignable — lecture seule", donnees.motif));

      blocs.push(
        h(
          "section",
          { class: "carte" },
          h(
            "header",
            { class: "carte__tete carte__tete--rangee" },
            h(
              "div",
              null,
              h("p", { class: "surtitre" }, "Utilisé par chaque génération"),
              h("h2", { class: "carte__titre" }, actif ? `Version active : ${actif.version}` : "Poids de repli (fallback-1)"),
              h("p", { class: "carte__sous-titre" }, actif ? actif.description || "Sans description." : "Aucun jeu actif en base : le moteur utilise les poids codés en dur, et trace « fallback-1 » dans chaque génération.")
            ),
            actif && donnees.base_joignable && h("button", { type: "button", class: "bouton bouton--fantome bouton--petit", onclick: revenirAuRepli }, "Revenir aux poids de repli")
          ),
          Graphiques.repartition(
            Object.entries(nombres(valeursActives)).map(([cle, valeur]) => ({ libelle: libelles[cle] || cle, valeur: valeur, couleur: Graphiques.COULEURS_SCORE[cle] })),
            { max: Number(donnees.maximum), format: (v) => UI.nombre(v, 2) }
          )
        )
      );

      const lignes = [
        {
          cellules: [
            h("span", null, h("strong", { class: "mono" }, "fallback-1"), h("span", { class: "secondaire" }, "poids codés dans recommandation.py")),
            Graphiques.empilee(nombres(donnees.repli.weights), { max: plafond, libelles: libelles }),
            UI.nombre(donnees.repli.runs),
            donnees.repli.is_active ? UI.badge("ok", "Actif") : UI.badge("inconnu", "Repli"),
            "",
          ],
        },
      ].concat(
        donnees.jeux.map((jeu) => ({
          cellules: [
            h("span", null, h("strong", { class: "mono" }, jeu.version), h("span", { class: "secondaire" }, jeu.description || "")),
            Graphiques.empilee(nombres(jeu.effectifs), { max: plafond, libelles: libelles }),
            UI.nombre(jeu.runs),
            jeu.is_active ? UI.badge("ok", "Actif") : jeu.ignores.length ? UI.badge("alerte", `Clés ignorées : ${jeu.ignores.join(", ")}`) : UI.badge("inconnu", "Inactif"),
            !jeu.is_active && donnees.base_joignable && !jeu.ignores.length ? h("button", { type: "button", class: "bouton bouton--contour bouton--mini", onclick: (e) => activer(jeu.version, e.currentTarget) }, "Activer") : "",
          ],
        }))
      );

      blocs.push(
        h(
          "section",
          { class: "carte" },
          h("header", { class: "carte__tete" }, h("h2", { class: "carte__titre" }, "Versions"), h("p", { class: "carte__sous-titre" }, "Immuables : on crée une nouvelle version, on ne modifie jamais une version déjà citée par une génération.")),
          h("div", { class: "pile" }, Graphiques.legendeScore(libelles), UI.tableau(["Version", "Poids", "Générations", "État", ""], lignes, { numeriques: [2] }))
        )
      );

      blocs.push(formulaireVersion(donnees, libelles, valeursActives));
      remplacer(zones.poids, blocs);
    } catch (erreur) {
      remplacer(zones.poids, UI.blocErreur(erreur));
    }
  }

  function formulaireVersion(donnees, libelles, depart) {
    const version = h("input", { type: "text", class: "champ__saisie champ__saisie--mono", placeholder: "v2", value: `v${donnees.jeux.length + 2}`, maxlength: 32 });
    const description = h("input", { type: "text", class: "champ__saisie", placeholder: "Pourquoi cette version ?", maxlength: 500 });
    const curseurs = {};
    const total = h("output");
    const majTotal = () => (total.textContent = UI.nombre(Object.values(curseurs).reduce((t, c) => t + Number(c.value), 0), 2));

    const lignes = donnees.autorises.map((a) => {
      const valeur = Number((depart || {})[a.cle] || a.defaut);
      const curseur = h("input", { type: "range", min: 0, max: Number(donnees.maximum), step: 0.05, value: valeur, "aria-label": a.libelle });
      const sortie = h("output", null, valeur.toFixed(2));
      curseur.addEventListener("input", () => {
        sortie.textContent = Number(curseur.value).toFixed(2);
        majTotal();
      });
      curseurs[a.cle] = curseur;
      return [h("span", null, h("i", { style: { display: "inline-block", width: "10px", height: "10px", borderRadius: "3px", marginRight: "6px", background: Graphiques.COULEURS_SCORE[a.cle] } }), a.libelle), curseur, sortie];
    });
    majTotal();

    const creer = h("button", { type: "button", class: "bouton bouton--primaire", disabled: !donnees.base_joignable }, icone("add"), "Créer la version");
    creer.addEventListener("click", async () => {
      UI.occuper(creer, true);
      try {
        const reponse = await Api.post(`${API}/scoring-weights`, {
          version: version.value.trim(),
          description: description.value.trim() || null,
          weights: Object.fromEntries(Object.entries(curseurs).map(([cle, c]) => [cle, Number(c.value).toFixed(2)])),
        });
        UI.toast(`Version « ${reponse.version} » créée, inactive. La comparer dans le laboratoire avant de l'activer.`, "succes", 9000);
        chargerPoids();
      } catch (erreur) {
        UI.toast(UI.messageErreur(erreur), "erreur", 9000);
        UI.occuper(creer, false);
      }
    });

    return h(
      "section",
      { class: "carte" },
      h("header", { class: "carte__tete" }, h("h2", { class: "carte__titre" }, "Nouvelle version"), h("p", { class: "carte__sous-titre" }, "Seules les clés lues par le moteur sont proposées. Les poids pondèrent des composantes comprises entre 0 et 1 : seul leur rapport change le classement.")),
      h(
        "div",
        { class: "pile" },
        h("div", { class: "grille grille--2" }, UI.champ("Version", version, "Minuscules, chiffres, « . », « _ » ou « - »."), UI.champ("Description", description)),
        h("div", { class: "poids-saisie" }, lignes),
        h("p", { class: "aide" }, "Somme des poids : ", total),
        h(
          "div",
          { class: "repli" },
          h("p", { class: "etiquette" }, "Prévus par FN-020, non lus par le moteur"),
          h("div", { class: "choix" }, donnees.prevus.map((p) => h("span", { class: "badge badge--inconnu", title: p.libelle }, icone("cadenas", "icone--xs"), p.cle))),
          h("p", { class: "aide", style: { marginTop: "8px" } }, "Les enregistrer n'aurait aucun effet tant que le score ne les calcule pas : l'API les refuse.")
        ),
        h("div", { class: "rangee rangee--ecart" }, h("a", { href: "/pilotage/laboratoire" }, icone("laboratoire", "icone--xs"), " Comparer dans le laboratoire"), creer)
      )
    );
  }

  async function activer(version, bouton) {
    const ok = await UI.confirmer({
      titre: `Activer « ${version} »`,
      libelle: "Activer",
      corps: [h("p", null, "Toutes les générations suivantes utiliseront ces poids, et la version sera tracée dans chacune."), h("p", { class: "aide" }, "La version active précédente est désactivée ; elle reste consultable et réactivable.")],
    });
    if (!ok) return;
    UI.occuper(bouton, true);
    try {
      await Api.post(`${API}/scoring-weights/${encodeURIComponent(version)}/activate`);
      UI.toast(`« ${version} » est active.`, "succes");
      chargerPoids();
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 9000);
      UI.occuper(bouton, false);
    }
  }

  async function revenirAuRepli() {
    const ok = await UI.confirmer({ titre: "Revenir aux poids de repli", libelle: "Désactiver", danger: true, corps: h("p", null, "Aucun jeu ne sera actif : le moteur utilisera les poids codés en dur (fallback-1).") });
    if (!ok) return;
    try {
      await Api.post(`${API}/scoring-weights/fallback`);
      UI.toast("Poids de repli rétablis.", "succes");
      chargerPoids();
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 9000);
    }
  }

  // --------------------------------------------------------------------------
  // Environnement
  // --------------------------------------------------------------------------

  function carteCles(titre, paires) {
    return h(
      "section",
      { class: "carte" },
      h("header", { class: "carte__tete" }, h("h2", { class: "carte__titre" }, titre)),
      h("dl", { class: "liste-cles" }, paires.map(([cle, valeur]) => [h("dt", null, cle), h("dd", null, valeur === null || valeur === undefined || valeur === "" ? "—" : valeur)]))
    );
  }

  async function chargerEnvironnement() {
    remplacer(zones.environnement, h("div", { class: "carte" }, UI.chargement(8)));
    try {
      const d = await Api.get(`${API}/environment`);
      remplacer(
        zones.environnement,
        h(
          "div",
          { class: "grille grille--2" },
          carteCles("Service", [
            ["Environnement", d.service.environnement],
            ["Niveau de journal", d.service.niveau_journal],
            ["Écoute", h("span", null, UI.code(d.service.ecoute), " ", d.service.expose_reseau ? UI.badge("alerte", "Exposé au réseau local") : UI.badge("ok", "Local uniquement"))],
          ]),
          carteCles("Base de données", [
            ["Hôte", UI.code(`${d.base.hote}:${d.base.port}`)],
            ["Base · utilisateur", `${d.base.nom} · ${d.base.utilisateur}`],
            ["Mot de passe", d.base.mot_de_passe_configure ? "configuré (jamais affiché)" : UI.badge("alerte", "absent")],
            ["Connexion", d.base.joignable ? UI.badge("ok", "Joignable") : h("span", null, UI.badge("critique", "Injoignable"), h("span", { class: "secondaire" }, d.base.motif || ""))],
            ["Migration", h("span", null, UI.code(d.base.revision || "—"), " → tête ", UI.code(d.base.tete || "—"), " ", d.base.a_jour ? UI.badge("ok", "À jour") : UI.badge("alerte", "alembic upgrade head"))],
          ]),
          carteCles("Authentification", [
            ["JWKS", UI.code(d.authentification.jwks_url)],
            ["Émetteur · audience", `${d.authentification.emetteur} · ${d.authentification.audience}`],
            ["JWKS NestJS", d.authentification.jwks_joignable ? UI.badge("ok", "Joignable") : h("span", null, UI.badge("alerte", "Injoignable"), h("span", { class: "secondaire" }, `${d.authentification.jwks_detail} — attendu avant la migration RS256 (sprint 01)`))],
            ["Clés de développement", d.authentification.cle_dev_privee ? UI.badge("alerte", "Configurées — refusées en production") : UI.badge("inconnu", "Absentes")],
          ]),
          carteCles("Fournisseur IA", [
            ["Modèle", UI.code(d.llm.modele)],
            ["Clé", d.llm.cle_configuree ? "configurée (jamais affichée)" : UI.badge("inconnu", "absente")],
            ["Délai", `${d.llm.delai_s} s`],
            ["Usage", "Aucun appel en génération (v1, sprint 08)"],
          ]),
          carteCles("Collecte", [
            ["Agent annoncé", UI.code(d.collecte.agent)],
            ["Contact", d.collecte.contact_renseigne ? UI.badge("ok", "Renseigné") : UI.badge("alerte", "SCRAPING_CONTACT vide")],
            ["Instantanés", UI.code(d.collecte.dossier_instantanes)],
          ]),
          carteCles("Observabilité", [["Sentry", d.observabilite.sentry_configure ? UI.badge("ok", "Configuré") : UI.badge("inconnu", "Non configuré")]])
        ),
        h("p", { class: "aide" }, icone("cadenas", "icone--xs"), " Lecture seule : ces valeurs viennent de .env et exigent un redémarrage. Aucun secret n'est affiché.")
      );
    } catch (erreur) {
      remplacer(zones.environnement, UI.blocErreur(erreur));
    }
  }

  const chargeurs = { parametres: chargerParametres, poids: chargerPoids, environnement: chargerEnvironnement };
  UI.onglets(document.getElementById("onglets"), (nom) => {
    if (!charges[nom]) {
      charges[nom] = true;
      chargeurs[nom]();
    }
  });
})();
