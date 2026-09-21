/* Suivi d'une exécution — sondage toutes les 1,5 s tant qu'elle tourne, puis
 * chargement des offres et des erreurs. Le journal est en ajout seul : on ne
 * demande que les événements postérieurs au dernier reçu. */
(function () {
  "use strict";

  const { h, icone, remplacer } = UI;
  const API = "/api/v1/admin/scraping";
  const racine = document.getElementById("execution");
  const runId = racine.dataset.run;
  const zones = {
    tete: document.getElementById("run-tete"),
    alertes: document.getElementById("run-alertes"),
    compteurs: document.getElementById("run-compteurs"),
    journal: document.getElementById("run-journal"),
    erreurs: document.getElementById("run-erreurs"),
    offres: document.getElementById("run-offres"),
    carteOffres: document.getElementById("run-offres-carte"),
  };

  const TYPES = {
    "scraping.structure": "Relevé de structure",
    "scraping.collecte": "Collecte à blanc",
    "scraping.rejeu": "Rejeu sur instantanés",
  };
  const ETAPES = [
    ["preparation", "Préparation"],
    ["robots", "robots.txt"],
    ["telechargement", "Téléchargement"],
    ["extraction", "Extraction"],
    ["appariement", "Appariement"],
    ["rapport", "Rapport"],
  ];
  const LIBELLES_ETAPES = Object.assign(Object.fromEntries(ETAPES), { attente: "Attente polie" });
  const EN_COURS = ["pending", "running"];
  const NIVEAUX = { info: "info", warning: "warning", error: "error" };

  let dernierSeq = 0;
  let run = null;
  let finalise = false;
  let filtre = "toutes";
  let premierChargement = true;

  function mode(r) {
    return (r.params && r.params.mode) || (r.kind === "scraping.rejeu" ? "rejeu" : "structure");
  }

  // --------------------------------------------------------------------------
  // Rendu
  // --------------------------------------------------------------------------

  function etapes(r) {
    const liste = ETAPES.filter(([cle]) => cle !== "appariement" || mode(r) !== "structure");
    const courante = r.step === "attente" ? "telechargement" : r.step;
    const index = liste.findIndex(([cle]) => cle === courante);
    return h(
      "ol",
      { class: "etapes", "aria-label": "Étapes" },
      liste.map(([cle, libelle], i) => {
        let etat = "";
        if (r.status === "succeeded") etat = "fait";
        else if (i < index) etat = "fait";
        else if (i === index) etat = EN_COURS.includes(r.status) ? "courant" : r.status === "failed" ? "echec" : "";
        return h(
          "li",
          { class: "etapes__etape" + (etat ? " etapes__etape--" + etat : "") },
          h("span", { class: "etapes__pastille" }, etat === "fait" ? icone("check", "icone--xs") : etat === "echec" ? icone("close", "icone--xs") : null),
          libelle
        );
      })
    );
  }

  function rendreTete(r, instantanes) {
    const enCours = EN_COURS.includes(r.status);
    const total = r.total || 0;
    const ratio = r.status === "succeeded" ? 1 : total ? Math.min(1, r.done / total) : 0;
    const classeProgression = enCours ? "progression--active" : r.status === "succeeded" ? "progression--ok" : r.status === "failed" ? "progression--echec" : "";
    const actions = [];
    if (enCours) {
      actions.push(h("button", { type: "button", class: "bouton bouton--contour", disabled: r.cancel_requested, onclick: annuler }, icone("stop"), r.cancel_requested ? "Arrêt demandé…" : "Annuler"));
    } else {
      if (instantanes) actions.push(h("button", { type: "button", class: "bouton bouton--contour", onclick: rejouer }, icone("replay"), "Rejouer l'extraction"));
      if (mode(r) !== "rejeu") actions.push(h("button", { type: "button", class: "bouton bouton--primaire", onclick: relancer }, icone("play"), "Relancer"));
    }

    remplacer(
      zones.tete,
      h(
        "div",
        { class: "rangee rangee--ecart" },
        h(
          "div",
          null,
          h("p", { class: "surtitre" }, TYPES[r.kind] || r.kind),
          h("h2", { class: "run__titre" }, r.subject || "—"),
          h("p", { class: "aide" }, `Demandée par ${r.requested_by || "—"} · créée le ${UI.date(r.created_at)}${r.started_at ? ` · durée ${UI.duree(r.duration_ms)}` : ""}`)
        ),
        h("div", { class: "rangee" }, UI.badgeStatut(r.status), r.alert && UI.badge("alerte", "Alerte structure"), actions)
      ),
      h(
        "div",
        { class: "pile" },
        h("div", { class: "rangee rangee--ecart" }, h("strong", null, enCours ? `${LIBELLES_ETAPES[r.step] || "En attente du démarrage"}…` : "Terminée"), h("span", { class: "aide" }, total ? `${UI.nombre(r.done)} / ${UI.nombre(total)} page(s)` : "")),
        h("div", { class: "progression " + classeProgression, role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(ratio * 100) }, h("div", { class: "progression__barre", style: { width: `${Math.round(ratio * 100)}%` } }))
      ),
      etapes(r)
    );
  }

  function rendreAlertes(r) {
    const alertes = [];
    if (r.status === "failed") alertes.push(UI.bandeau("erreur", `Échec : ${r.error_code || "motif inconnu"}`, r.error_detail));
    if (r.status === "interrupted") alertes.push(UI.bandeau("alerte", "Exécution interrompue", r.error_detail));
    if (r.status === "cancelled") alertes.push(UI.bandeau("info", "Exécution annulée", "L'arrêt a eu lieu avant la page suivante ; ce qui a déjà été lu est conservé."));
    if (r.alert) {
      alertes.push(
        UI.bandeau(
          "alerte",
          "Alerte de changement de structure (FN-013)",
          "Trop de prix affichés sont illisibles, ou une page ne présente aucun produit reconnu. Inspecter les offres et le journal ; après correction du connecteur, rejouer l'extraction sur les instantanés."
        )
      );
    }
    remplacer(zones.alertes, alertes.length ? h("div", { class: "pile" }, alertes) : null);
  }

  function rendreCompteurs(r) {
    const c = r.counters || {};
    const taux = c.taux_echec_extraction;
    const robots = c.robots_autorise;
    const cartes = [
      { titre: "Pages lues", valeur: c.pages_total !== undefined ? `${UI.nombre(c.pages_lues)}/${UI.nombre(c.pages_total)}` : "—", etat: "aubergine", sous: c.pages_ignorees ? `${c.pages_ignorees} ignorée(s)` : null },
      { titre: "Produits", valeur: UI.nombre(c.produits), etat: "aubergine", sous: "blocs produit reconnus" },
      { titre: "Prix affichés", valeur: UI.nombre(c.prix_trouves), etat: "aubergine", sous: (c.exemples_prix || []).slice(0, 2).join(" · ") || null },
      { titre: "Prix lus", valeur: UI.nombre(c.prix_lus), etat: "aubergine", sous: "montants en ariary" },
      { titre: "Prix illisibles", valeur: UI.nombre(c.prix_illisibles), etat: c.prix_illisibles ? "alerte" : "ok", sous: taux === undefined || taux === null ? "aucun taux : rien à lire" : `taux d'échec ${UI.pourcentage(taux, 1)}` },
    ];
    if (mode(r) !== "structure") {
      cartes.push(
        { titre: "Offres", valeur: UI.nombre(c.offres), etat: "aubergine", sous: "conservées en transit" },
        { titre: "Appariées", valeur: UI.nombre(c.appariees), etat: "ok", sous: `${UI.nombre(c.ingredients_apparies)} ingrédient(s) distinct(s)` },
        { titre: "À revoir", valeur: UI.nombre(c.non_appariees), etat: c.non_appariees ? "alerte" : "ok", sous: "file de revue" }
      );
    }
    cartes.push(
      { titre: "robots.txt", valeur: robots === true ? "Autorise" : robots === false ? "Interdit" : "—", etat: robots === true ? "ok" : robots === false ? "critique" : "inconnu", sous: "relu à chaque exécution" },
      { titre: "Erreurs", valeur: UI.nombre(c.erreurs), etat: c.erreurs ? "critique" : "ok", sous: c.octets ? `${UI.octets(c.octets)} téléchargés` : null }
    );
    remplacer(zones.compteurs, cartes.map((k) => UI.carteStat(k.titre, k.valeur, k.etat, k.sous)));
  }

  function ajouterEvenement(e) {
    const donnees = e.data || {};
    const enBas = zones.journal.scrollHeight - zones.journal.scrollTop - zones.journal.clientHeight < 40;
    zones.journal.appendChild(
      h(
        "li",
        { class: "journal__ligne journal__ligne--" + (NIVEAUX[e.level] || "info") },
        h("span", { class: "journal__heure" }, UI.heure(e.at)),
        icone(NIVEAUX[e.level] || "info"),
        h("span", null, e.step && h("span", { class: "journal__etape" }, LIBELLES_ETAPES[e.step] || e.step), e.message, donnees.url && h("span", { class: "journal__lien" }, UI.lien(donnees.url)))
      )
    );
    if (enBas) zones.journal.scrollTop = zones.journal.scrollHeight;
  }

  // --------------------------------------------------------------------------
  // Offres et erreurs
  // --------------------------------------------------------------------------

  async function chargerOffres() {
    if (mode(run) === "structure") {
      remplacer(zones.offres, UI.vide("Aucune offre conservée", "Un relevé de structure compte les produits et les prix affichés : il ne garde aucune offre. Lancer une collecte à blanc pour inspecter les libellés.", "info"));
      return;
    }
    remplacer(zones.offres, UI.chargement(6));
    try {
      const { offres, total } = await Api.get(`${API}/runs/${runId}/offers?filtre=${filtre}&limite=1000`);
      if (!total) {
        remplacer(zones.offres, UI.vide("Aucune offre", filtre === "toutes" ? "Cette exécution n'a extrait aucune offre." : "Aucune offre ne correspond à ce filtre.", "collecte"));
        return;
      }
      remplacer(
        zones.offres,
        UI.tableau(
          ["Libellé", "Prix affiché", "Prix lu", "Conditionnement", "Ingrédient"],
          offres.map((o) => ({
            cellules: [
              h("span", null, o.url ? UI.lien(o.url, o.libelle) : o.libelle, o.disponibilite && h("span", { class: "secondaire" }, o.disponibilite)),
              o.prix_texte || h("span", { class: "statut-prix" }, "aucun"),
              o.statut_prix === "read" ? h("strong", null, UI.ariary(o.prix)) : h("span", null, UI.badge(o.statut_prix === "absent" ? "inconnu" : "alerte", o.statut_prix === "absent" ? "Absent" : "Illisible"), h("span", { class: "secondaire" }, o.motif_prix || "")),
              o.quantite !== null ? `${UI.nombre(o.quantite, 3)} ${o.unite}` : h("span", { class: "statut-prix" }, o.conditionnement || "non lu"),
              o.statut_appariement === "matched" ? h("span", null, UI.badge("ok", o.ingredient), h("span", { class: "secondaire" }, o.ingredient_nom || "")) : h("a", { href: "/pilotage/collecte#revue" }, UI.badge("alerte", "À revoir")),
            ],
          })),
          { numeriques: [2] }
        )
      );
    } catch (erreur) {
      remplacer(zones.offres, UI.blocErreur(erreur));
    }
  }

  async function chargerErreurs() {
    try {
      const { erreurs } = await Api.get(`${API}/errors?run_id=${runId}`);
      remplacer(
        zones.erreurs,
        erreurs.length
          ? UI.tableau(
              ["Étape", "HTTP", "Message"],
              erreurs.map((e) => ({ cellules: [e.etape, e.http_status || "—", h("span", null, e.message, e.url && h("span", { class: "secondaire" }, UI.lien(e.url)))] }))
            )
          : UI.vide("Aucune erreur", null, "check")
      );
    } catch (erreur) {
      remplacer(zones.erreurs, UI.blocErreur(erreur));
    }
  }

  // --------------------------------------------------------------------------
  // Actions
  // --------------------------------------------------------------------------

  async function annuler() {
    const ok = await UI.confirmer({
      titre: "Annuler l'exécution",
      libelle: "Demander l'arrêt",
      danger: true,
      corps: h("p", null, "L'exécution s'arrêtera avant la page suivante. Ce qui a déjà été relevé reste consultable."),
    });
    if (!ok) return;
    try {
      await Api.post(`${API}/runs/${runId}/cancel`);
      UI.toast("Arrêt demandé.", "info");
      sonder();
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
    }
  }

  async function rejouer() {
    const ok = await UI.confirmer({
      titre: "Rejouer l'extraction",
      libelle: "Rejouer",
      corps: [h("p", null, "L'extraction et l'appariement sont relancés sur les pages conservées de cette exécution."), h("p", { class: "aide" }, "Aucune requête n'est envoyée au site. Utile après la correction d'un connecteur ou l'ajout d'alias.")],
    });
    if (!ok) return;
    try {
      const r = await Api.post(`${API}/runs/${runId}/replay`);
      location.href = `/pilotage/collecte/executions/${r.run_id}`;
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
    }
  }

  async function relancer() {
    const m = mode(run);
    const ok = await UI.confirmer({
      titre: `Relancer — ${run.subject}`,
      libelle: "Relancer",
      corps: h("p", null, m === "structure" ? "Un nouveau relevé de structure sera effectué (une page)." : "Une nouvelle collecte à blanc sera effectuée, avec les mêmes garde-fous."),
    });
    if (!ok) return;
    try {
      const r = await Api.post(`${API}/run`, { source: run.subject, mode: m, pages: (run.params && run.params.pages && run.params.pages.length ? run.params.pages : null) });
      if (r.avertissement) UI.toastApresNavigation(r.avertissement, "alerte");
      location.href = `/pilotage/collecte/executions/${r.run_id}`;
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
    }
  }

  // --------------------------------------------------------------------------
  // Sondage
  // --------------------------------------------------------------------------

  async function sonder() {
    try {
      const donnees = await Api.get(`${API}/runs/${runId}?apres=${dernierSeq}`);
      run = donnees.run;
      if (premierChargement) {
        premierChargement = false;
        remplacer(zones.journal);
      }
      donnees.events.forEach(ajouterEvenement);
      if (donnees.events.length) dernierSeq = donnees.events[donnees.events.length - 1].seq;
      if (!zones.journal.children.length) zones.journal.appendChild(h("li", { class: "aide" }, "En attente des premiers événements…"));
      else Array.from(zones.journal.querySelectorAll("li.aide")).forEach((li) => li.remove());

      rendreTete(run, donnees.instantanes);
      rendreAlertes(run);
      rendreCompteurs(run);

      if (EN_COURS.includes(run.status)) {
        setTimeout(sonder, 1500);
      } else if (!finalise) {
        finalise = true;
        chargerOffres();
        chargerErreurs();
      }
    } catch (erreur) {
      remplacer(zones.tete, UI.blocErreur(erreur));
      if (!erreur.status || erreur.status >= 500) setTimeout(sonder, 4000);
    }
  }

  UI.segmente(document.getElementById("filtre-offres"), (valeur) => {
    filtre = valeur;
    if (run) chargerOffres();
  });

  remplacer(zones.tete, UI.chargement(4));
  remplacer(zones.erreurs, UI.chargement(2));
  remplacer(zones.offres, UI.chargement(4));
  sonder();
})();
