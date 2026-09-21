/* Collecte — sources, qualification SPIKE-01, exécutions, file de revue, erreurs.
 * Lancer une exécution ouvre sa page de suivi en direct. */
(function () {
  "use strict";

  const { h, icone, remplacer } = UI;
  const API = "/api/v1/admin/scraping";
  const zones = {
    verdict: document.getElementById("verdict"),
    sources: document.getElementById("sources"),
    executions: document.getElementById("executions"),
    revue: document.getElementById("revue"),
    erreurs: document.getElementById("erreurs"),
  };
  const TYPES = {
    "scraping.structure": "Relevé de structure",
    "scraping.collecte": "Collecte à blanc",
    "scraping.rejeu": "Rejeu sur instantanés",
  };
  const PLATEFORMES = { prestashop: "PrestaShop", woocommerce: "WooCommerce" };
  const charges = {};
  let contexte = null;

  // --------------------------------------------------------------------------
  // Sources et qualification
  // --------------------------------------------------------------------------

  async function chargerSources() {
    remplacer(zones.verdict, UI.chargement(3));
    remplacer(zones.sources, h("div", { class: "carte" }, UI.chargement(8)));
    try {
      const [sources, qualification] = await Promise.all([Api.get(`${API}/sources`), Api.get(`${API}/qualification`)]);
      contexte = { sources: sources, qualification: qualification };
      rendreVerdict();
      rendreSources();
    } catch (erreur) {
      remplacer(zones.verdict, UI.blocErreur(erreur));
      remplacer(zones.sources);
    }
  }

  function rendreVerdict() {
    const q = contexte.qualification;
    const s = contexte.sources;
    const sansReleve = q.sources.every((src) => !src.serie_structure.length);
    remplacer(
      zones.verdict,
      h(
        "div",
        { class: "verdict" },
        Graphiques.anneau(q.requises ? Math.min(1, q.qualifiees / q.requises) : null, {
          texte: `${q.qualifiees}/${q.requises}`,
          couleur: q.qualifiees >= q.requises ? Graphiques.COULEURS.vert : Graphiques.COULEURS.rose,
          libelle: "Sources qualifiées",
        }),
        h(
          "div",
          { class: "verdict__texte" },
          h("p", { class: "surtitre" }, "SPIKE-01 · condition d'entrée du lot 4"),
          h("h2", { class: "verdict__titre" }, q.verdict),
          h("p", { class: "aide" }, "Chaque critère est déduit des relevés archivés. Aucun ne passe au vert par déclaration."),
          h(
            "div",
            { class: "rangee" },
            h("span", { class: "puce " + (s.contact_renseigne ? "puce--ok" : "puce--alerte"), title: s.agent }, icone("shield"), s.contact_renseigne ? "Contact du robot renseigné" : "Contact non renseigné (SCRAPING_CONTACT)"),
            h("span", { class: "puce" }, icone("history"), `Délai plancher ${s.delai_plancher_s} s · ${s.plafond_pages} pages au plus`),
            sansReleve && h("button", { type: "button", class: "bouton bouton--contour bouton--petit", onclick: importerArchive }, icone("history"), "Importer les relevés de spike01_releve.py")
          )
        )
      )
    );
  }

  function rendreSources() {
    const sources = contexte.sources.sources;
    if (!sources.length) {
      remplacer(zones.sources, UI.vide("Aucune source en base", "Appliquer la migration (alembic upgrade head) : elle insère kibo.mg et abcie.org.", "collecte"));
      return;
    }
    const qualif = Object.fromEntries(contexte.qualification.sources.map((q) => [q.slug, q]));
    remplacer(zones.sources, sources.map((src) => carteSource(src, qualif[src.slug])));
  }

  function critere(c) {
    return h(
      "li",
      { class: "critere critere--" + c.etat },
      h("span", { class: "critere__numero" }, String(c.numero)),
      h(
        "div",
        null,
        h("p", { class: "critere__libelle" }, c.libelle, " ", UI.badge(c.etat)),
        h("p", { class: "critere__preuve" }, c.preuve),
        c.prochaine_etape && h("p", { class: "critere__suite" }, icone("chevron", "icone--xs"), c.prochaine_etape)
      )
    );
  }

  function carteSource(src, q) {
    const derniere = src.derniere_execution;
    return h(
      "article",
      { class: "carte source" + (src.is_active ? "" : " est-inactive") },
      h(
        "header",
        { class: "source__tete" },
        h("div", null, h("p", { class: "surtitre" }, PLATEFORMES[src.platform] || src.platform), h("h3", { class: "source__nom" }, src.name), UI.lien(src.base_url)),
        h("div", { class: "rangee" }, q && (q.qualifiee ? UI.badge("ok", "Qualifiée") : UI.badge("inconnu", "Non qualifiée")), !src.is_active && UI.badge("alerte", "Désactivée"))
      ),
      q && h("ol", { class: "criteres" }, q.criteres.map(critere)),
      q && q.serie_structure.length
        ? h(
            "div",
            null,
            h("p", { class: "etiquette" }, "Relevés de structure — prix trouvés"),
            Graphiques.releves(q.serie_structure),
            q.prochain_releve && h("p", { class: "aide" }, q.releve_du ? UI.badge("alerte", "Relevé dû") : null, " Prochain relevé compté à partir du ", h("strong", null, UI.date(q.prochain_releve, false)))
          )
        : h("p", { class: "aide" }, "Aucun relevé de structure archivé pour cette source."),
      derniere &&
        h(
          "p",
          { class: "source__derniere" },
          "Dernière exécution : ",
          h("a", { href: `/pilotage/collecte/executions/${derniere.id}` }, `${TYPES[derniere.kind] || derniere.kind}, ${UI.date(derniere.created_at)}`),
          " ",
          UI.badgeStatut(derniere.status)
        ),
      h(
        "div",
        { class: "source__actions" },
        h("button", { type: "button", class: "bouton bouton--primaire", disabled: !src.is_active, onclick: () => lancer(src, "structure") }, icone("play"), "Relevé de structure"),
        h("button", { type: "button", class: "bouton bouton--contour", disabled: !src.is_active, onclick: () => lancer(src, "collecte") }, icone("collecte"), "Collecte à blanc")
      ),
      configuration(src)
    );
  }

  function configuration(src) {
    const pages = h("textarea", { class: "champ__saisie champ__saisie--mono", rows: 3, spellcheck: "false" });
    pages.value = src.pages.join("\n");
    const delai = h("input", { type: "number", class: "champ__saisie", min: 3, max: 120, step: 0.5, value: src.delay_seconds });
    const maxPages = h("input", { type: "number", class: "champ__saisie", min: 1, max: 50, step: 1, value: src.max_pages });
    const cgu = h("input", { type: "url", class: "champ__saisie", placeholder: "https://… (CGU lues)", value: src.tos_url || "" });
    const actif = h("input", { type: "checkbox", class: "interrupteur" });
    actif.checked = src.is_active;

    const enregistrer = h(
      "button",
      {
        type: "button",
        class: "bouton bouton--primaire bouton--petit",
        onclick: (e) =>
          modifier(src, e.currentTarget, {
            pages: pages.value.split(/\n+/).map((p) => p.trim()).filter(Boolean),
            delay_seconds: delai.value,
            max_pages: Number(maxPages.value),
            is_active: actif.checked,
            tos_url: cgu.value.trim(),
          }),
      },
      "Enregistrer"
    );

    return h(
      "details",
      { class: "source__config" },
      h("summary", null, icone("reglages"), "Configuration et conformité"),
      h(
        "div",
        { class: "pile" },
        h("div", { class: "grille grille--2" }, UI.champ("Délai entre deux pages (s)", delai, "Le plancher de 3 s est imposé par le code et par la base."), UI.champ("Pages maximales par collecte", maxPages)),
        UI.champ("Pages de catalogue, une par ligne", pages, `Uniquement des pages de ${src.base_url} — le relevé de structure lit la première.`),
        h(
          "div",
          { class: "repli pile" },
          h("p", { class: "etiquette" }, "Critère 4 — conditions d'utilisation"),
          src.tos_attested_by
            ? h("p", null, UI.badge("ok", "Attestées"), ` par ${src.tos_attested_by} le ${UI.date(src.tos_attested_at)}`)
            : h("p", { class: "aide" }, "Les CGU ne se lisent pas par programme : elles s'attestent, avec un nom et une date."),
          UI.champ("Lien vers les CGU lues", cgu),
          h(
            "div",
            { class: "rangee" },
            src.tos_attested_by
              ? h("button", { type: "button", class: "bouton bouton--fantome bouton--petit", onclick: (e) => modifier(src, e.currentTarget, { tos_attested: false }) }, "Retirer l'attestation")
              : h("button", { type: "button", class: "bouton bouton--secondaire bouton--petit", onclick: (e) => attester(src, e.currentTarget, cgu.value.trim()) }, icone("shield"), "J'ai lu les CGU : attester")
          )
        ),
        h("div", { class: "rangee rangee--ecart" }, h("label", { class: "interrupteur-champ" }, actif, "Source active"), enregistrer)
      )
    );
  }

  async function modifier(src, bouton, changements) {
    UI.occuper(bouton, true);
    try {
      await Api.patch(`${API}/sources/${encodeURIComponent(src.slug)}`, changements);
      UI.toast(`Source « ${src.name} » mise à jour.`, "succes");
      await chargerSources();
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
      UI.occuper(bouton, false);
    }
  }

  async function attester(src, bouton, lienCgu) {
    const ok = await UI.confirmer({
      titre: `Attester les CGU de ${src.name}`,
      libelle: "J'atteste",
      corps: [
        h("p", null, "Vous attestez avoir lu les conditions d'utilisation du site et qu'elles n'interdisent pas la collecte de prix affichés publiquement."),
        h("p", { class: "aide" }, "L'attestation est journalisée à votre nom. Elle ne remplace pas l'arbitrage de réutilisation commerciale des données, qui reste une décision d'équipe."),
      ],
    });
    if (ok) modifier(src, bouton, { tos_attested: true, tos_url: lienCgu });
  }

  async function lancer(src, mode) {
    const structure = mode === "structure";
    const ok = await UI.confirmer({
      titre: `${structure ? "Relevé de structure" : "Collecte à blanc"} — ${src.name}`,
      libelle: "Lancer",
      corps: [
        structure
          ? h("p", null, "Une seule page relevée : ", UI.code(src.pages[0] || "aucune page configurée"), ". Produits et prix affichés sont comptés, aucun prix n'est conservé.")
          : h("p", null, `Jusqu'à ${src.max_pages} page(s), à au moins ${src.delay_seconds} s d'intervalle. Les offres sont conservées en transit pour inspection — aucun prix n'est écrit.`),
        h(
          "ul",
          { class: "liste-verrous" },
          h("li", null, icone("check"), "robots.txt relu avant la première page ; une page interdite ou incertaine n'est pas relevée."),
          h("li", null, icone("check"), h("span", null, "Agent annoncé : ", UI.code(contexte.sources.agent))),
          h("li", null, icone("check"), "Requêtes GET uniquement, redirections hors du site refusées, aucun contournement de protection.")
        ),
      ],
    });
    if (!ok) return;
    try {
      const reponse = await Api.post(`${API}/run`, { source: src.slug, mode: mode });
      if (reponse.avertissement) UI.toastApresNavigation(reponse.avertissement, "alerte");
      location.href = `/pilotage/collecte/executions/${reponse.run_id}`;
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
    }
  }

  async function importerArchive(evenement) {
    const bouton = evenement.currentTarget;
    UI.occuper(bouton, true);
    try {
      const r = await Api.post(`${API}/import-spike01`);
      UI.toast(`${r.importes} relevé(s) importé(s), ${r.deja_presents} déjà présent(s)${r.sources_inconnues.length ? ` — sources inconnues : ${r.sources_inconnues.join(", ")}` : ""}.`, "succes", 8000);
      await chargerSources();
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur", 8000);
      UI.occuper(bouton, false);
    }
  }

  // --------------------------------------------------------------------------
  // Exécutions
  // --------------------------------------------------------------------------

  async function chargerExecutions() {
    remplacer(zones.executions, UI.chargement(5));
    try {
      const { runs } = await Api.get(`${API}/runs?limite=100`);
      if (!runs.length) {
        remplacer(zones.executions, UI.vide("Aucune exécution", "Lancer un relevé de structure depuis l'onglet des sources.", "history"));
        return;
      }
      remplacer(
        zones.executions,
        UI.tableau(
          ["Lancée", "Source", "Type", "Statut", "Pages", "Produits", "Prix lus", "Appariées", "Durée"],
          runs.map((r) => {
            const c = r.counters || {};
            return {
              cellules: [
                UI.date(r.created_at),
                r.subject || "—",
                TYPES[r.kind] || r.kind,
                h("span", { class: "rangee" }, UI.badgeStatut(r.status), r.alert && UI.badge("alerte", "Alerte structure")),
                c.pages_total !== undefined ? `${UI.nombre(c.pages_lues)}/${UI.nombre(c.pages_total)}` : "—",
                UI.nombre(c.produits),
                UI.nombre(c.prix_lus),
                r.kind === "scraping.structure" ? "—" : UI.nombre(c.appariees),
                UI.duree(r.duration_ms),
              ],
              lien: `/pilotage/collecte/executions/${r.id}`,
            };
          }),
          { numeriques: [4, 5, 6, 7, 8] }
        )
      );
    } catch (erreur) {
      remplacer(zones.executions, UI.blocErreur(erreur));
    }
  }

  // --------------------------------------------------------------------------
  // File de revue
  // --------------------------------------------------------------------------

  async function chargerRevue() {
    remplacer(zones.revue, UI.chargement(5));
    try {
      const donnees = await Api.get(`${API}/review`);
      const avertissement = donnees.ingredients.length
        ? null
        : UI.bandeau("alerte", "Le référentiel ne compte aucun ingrédient en base", ["Charger le catalogue versionné : ", UI.code("python -m app.seed"), "."]);
      if (!donnees.libelles.length) {
        remplacer(zones.revue, avertissement, UI.vide("File vide", "Aucun libellé en attente : tout est apparié, ou aucune collecte à blanc n'a encore eu lieu.", "check"));
        return;
      }
      remplacer(
        zones.revue,
        avertissement,
        UI.tableau(
          ["Libellé collecté", "Vu", "Sources", "Exemple de prix", "Associer à un ingrédient"],
          donnees.libelles.map((l) => ({
            cellules: [
              h("span", null, l.exemple, h("span", { class: "secondaire mono" }, l.libelle_normalise)),
              `${UI.nombre(l.occurrences)} fois`,
              l.sources.join(", ") || "—",
              l.exemple_prix || "—",
              formulaireAlias(l, donnees.ingredients),
            ],
          })),
          { numeriques: [1] }
        )
      );
    } catch (erreur) {
      remplacer(zones.revue, UI.blocErreur(erreur));
    }
  }

  function formulaireAlias(libelle, ingredients) {
    const choix = h("select", { class: "champ__saisie", "aria-label": "Ingrédient" }, h("option", { value: "" }, "Choisir…"), ingredients.map((i) => h("option", { value: i.slug }, i.name)));
    const bouton = h(
      "button",
      {
        type: "button",
        class: "bouton bouton--secondaire bouton--mini",
        disabled: !ingredients.length,
        onclick: async () => {
          if (!choix.value) {
            UI.toast("Choisir d'abord un ingrédient.", "alerte");
            return;
          }
          UI.occuper(bouton, true);
          try {
            const r = await Api.post(`${API}/review/aliases`, { label: libelle.exemple, ingredient: choix.value });
            UI.toast(`${r.alias_cree ? "Alias créé" : "Alias déjà connu"} — ${r.offres_reappariees} offre(s) réappariée(s) à ${r.ingredient}.`, "succes");
            chargerRevue();
          } catch (erreur) {
            UI.toast(UI.messageErreur(erreur), "erreur", 8000);
            UI.occuper(bouton, false);
          }
        },
      },
      "Associer"
    );
    return h("div", { class: "rangee" }, choix, bouton);
  }

  // --------------------------------------------------------------------------
  // Erreurs
  // --------------------------------------------------------------------------

  async function chargerErreurs() {
    remplacer(zones.erreurs, UI.chargement(5));
    try {
      const { erreurs } = await Api.get(`${API}/errors?limite=200`);
      if (!erreurs.length) {
        remplacer(zones.erreurs, UI.vide("Aucune erreur", "Aucune exécution n'a rencontré d'erreur.", "check"));
        return;
      }
      remplacer(
        zones.erreurs,
        UI.tableau(
          ["Quand", "Source", "Étape", "HTTP", "Message", "Page"],
          erreurs.map((e) => ({
            cellules: [UI.date(e.at), e.source || "—", e.etape, e.http_status || "—", e.message, e.url ? UI.lien(e.url, "ouvrir") : "—"],
            lien: `/pilotage/collecte/executions/${e.run_id}`,
          })),
          { numeriques: [3] }
        )
      );
    } catch (erreur) {
      remplacer(zones.erreurs, UI.blocErreur(erreur));
    }
  }

  // --------------------------------------------------------------------------

  const chargeurs = { sources: chargerSources, executions: chargerExecutions, revue: chargerRevue, erreurs: chargerErreurs };

  UI.onglets(document.getElementById("onglets"), (nom) => {
    if (!charges[nom]) {
      charges[nom] = true;
      chargeurs[nom]();
    }
  });
  document.querySelectorAll("[data-rafraichir]").forEach((bouton) => bouton.addEventListener("click", () => chargeurs[bouton.dataset.rafraichir]()));
})();
