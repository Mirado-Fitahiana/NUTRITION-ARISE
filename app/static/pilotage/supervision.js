/* Supervision — FN-035. Chaque indicateur affiche sa valeur, son état et sa
 * preuve. « Aucune donnée » s'affiche « — », jamais 0. */
(function () {
  "use strict";

  const { h, icone, remplacer } = UI;
  const API = "/api/v1/admin/supervision";
  const zones = {
    base: document.getElementById("bandeau-base"),
    securite: document.getElementById("bandeau-securite"),
    resume: document.getElementById("resume"),
    blocs: document.getElementById("blocs"),
    generations: document.getElementById("generations"),
    maj: document.getElementById("maj"),
  };
  let periode = 7;

  const LIBELLES_SCORE = { nutrition: "Adéquation calorique", cost: "Coût", preference: "Préférés", variety: "Variété", favorite: "Favori" };
  const OBJECTIFS = {
    weight_loss: "Perte de poids",
    weight_maintenance: "Maintien",
    weight_gain: "Prise de poids",
    muscle_gain: "Prise de muscle",
    balanced_diet: "Équilibre",
    habit_improvement: "Habitudes",
  };

  function valeur(indicateur) {
    const v = indicateur.valeur;
    if (v === null || v === undefined) {
      return h("span", { class: "indicateur__valeur est-inconnue", title: "Aucune donnée mesurable — ce n'est pas un zéro" }, "—");
    }
    let texte;
    if (indicateur.unite === "ratio") texte = UI.pourcentage(v, 1);
    else if (indicateur.unite === "s") texte = UI.nombre(v, 2) + " s";
    else if (indicateur.unite === "USD") texte = "$ " + UI.nombre(v, 4);
    else if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) texte = UI.date(v);
    else if (typeof v === "number") texte = UI.nombre(v, 2);
    else texte = String(v);
    return h("span", { class: "indicateur__valeur" }, texte);
  }

  function graphe(indicateur) {
    const serie = indicateur.serie;
    let donnees = serie;
    let series;
    if (indicateur.id === "generation.volume") {
      donnees = serie.map((p) => ({ x: p.x, autres: Math.max(0, p.total - p.echecs), echecs: p.echecs }));
      series = [
        { cle: "autres", libelle: "Réussies ou en cours", couleur: Graphiques.COULEURS.aubergine },
        { cle: "echecs", libelle: "Échecs", couleur: Graphiques.COULEURS.rose },
      ];
    } else if (indicateur.id === "cout.appels") {
      series = [{ cle: "appels", libelle: "Appels LLM", couleur: Graphiques.COULEURS.violet }];
    } else {
      const cle = Object.keys(serie[0]).find((k) => k !== "x");
      series = [{ cle: cle, libelle: cle, couleur: Graphiques.COULEURS.rose }];
    }
    return h("div", { class: "graphe-conteneur" }, Graphiques.barres(donnees, { series: series, libelle: indicateur.libelle, formatX: (x) => x.slice(8, 10) + "/" + x.slice(5, 7) }), Graphiques.legende(series));
  }

  function indicateur(ind) {
    const repartition = ind.repartition && ind.repartition.length ? Graphiques.repartition(ind.repartition, { format: (v) => UI.nombre(v) }) : null;
    return h(
      "article",
      { class: "indicateur indicateur--" + ind.etat },
      h("div", { class: "indicateur__tete" }, h("h3", { class: "indicateur__libelle" }, ind.libelle), UI.badge(ind.etat)),
      h("div", { class: "indicateur__corps" }, valeur(ind), ind.detail && h("p", { class: "indicateur__detail" }, ind.detail)),
      ind.serie && ind.serie.length ? graphe(ind) : null,
      repartition,
      h("p", { class: "indicateur__preuve", title: "Preuve de la mesure" }, icone("doc", "icone--xs"), ind.preuve)
    );
  }

  function bloc(b) {
    return h(
      "section",
      { class: "carte bloc", id: "bloc-" + b.id },
      h("header", { class: "carte__tete" }, h("h2", { class: "carte__titre" }, b.titre), h("p", { class: "carte__sous-titre" }, b.description)),
      h("div", { class: "indicateurs" }, b.indicateurs.map(indicateur))
    );
  }

  function rendre(donnees) {
    const r = donnees.resume;
    remplacer(
      zones.resume,
      UI.carteStat("Critiques", UI.nombre(r.critiques), r.critiques ? "critique" : "ok", "doivent rester à zéro"),
      UI.carteStat("Alertes", UI.nombre(r.alertes), r.alertes ? "alerte" : "ok", "à surveiller"),
      UI.carteStat("Inconnus", UI.nombre(r.inconnus), "inconnu", "aucune donnée mesurable"),
      UI.carteStat("Conformes", UI.nombre(r.ok), "ok", "mesurés et dans les normes")
    );

    remplacer(
      zones.base,
      donnees.base_joignable
        ? null
        : UI.bandeau("erreur", "Base de données injoignable — rien ne peut être mesuré", [
            h("p", null, "Vérifier ", UI.code("DATABASE_HOST"), ", ", UI.code("DATABASE_PORT"), " et le mot de passe dans ", UI.code(".env"), ", puis appliquer ", UI.code("alembic upgrade head"), "."),
            h("p", null, "Les indicateurs restent « inconnus » tant que la mesure est impossible : ils ne sont jamais affichés à zéro."),
          ])
    );

    const securite = donnees.blocs.find((b) => b.id === "securite");
    const critiques = securite.indicateurs.filter((i) => i.etat === "critique");
    const inconnus = securite.indicateurs.filter((i) => i.etat === "inconnu");
    if (critiques.length) {
      remplacer(
        zones.securite,
        UI.bandeau(
          "erreur",
          "Incident de sécurité alimentaire — traiter comme un incident de production",
          critiques.map((i) => h("p", null, `${i.libelle} : ${UI.nombre(i.valeur)} — ${i.preuve}`))
        )
      );
    } else if (inconnus.length) {
      remplacer(zones.securite, UI.bandeau("alerte", "Sécurité alimentaire non mesurée", inconnus[0].preuve));
    } else {
      remplacer(
        zones.securite,
        h("div", { class: "bandeau bandeau--securite-ok" }, icone("shield", "icone--lg"), h("div", { class: "bandeau__texte" }, h("p", { class: "bandeau__titre" }, "Aucun incident de sécurité alimentaire"), h("p", null, securite.indicateurs.map((i) => `${i.libelle} : ${UI.nombre(i.valeur)}`).join(" · "))))
      );
    }

    remplacer(zones.blocs, donnees.blocs.filter((b) => b.id !== "securite").map(bloc));
    zones.maj.textContent = `Mesuré le ${UI.date(donnees.genere_le)} · ${donnees.periode_jours} jour(s)`;
  }

  async function charger() {
    const bouton = document.getElementById("actualiser");
    UI.occuper(bouton, true);
    remplacer(zones.blocs, h("div", { class: "carte" }, UI.chargement(6)), h("div", { class: "carte" }, UI.chargement(6)));
    try {
      rendre(await Api.get(`${API}/indicateurs?periode=${periode}`));
    } catch (erreur) {
      remplacer(zones.resume);
      remplacer(zones.blocs, UI.blocErreur(erreur));
      zones.maj.textContent = "Mesure impossible";
    } finally {
      UI.occuper(bouton, false);
    }
    chargerGenerations();
  }

  async function chargerGenerations() {
    remplacer(zones.generations, UI.chargement(4));
    try {
      const donnees = await Api.get(`${API}/generations?limite=30`);
      if (!donnees.generations.length) {
        remplacer(zones.generations, UI.vide("Aucune génération", "Le mobile n'a encore demandé aucun programme à ce service.", "plat"));
        return;
      }
      remplacer(
        zones.generations,
        UI.tableau(
          ["Lancée", "Statut", "Objectif", "Jours", "Durée", "Motif d'échec", "Utilisateur"],
          donnees.generations.map((g) => ({
            cellules: [
              UI.date(g.created_at),
              UI.badgeStatut(g.status),
              OBJECTIFS[g.goal] || g.goal || "—",
              UI.nombre(g.days),
              UI.duree(g.duree_ms),
              g.error_code ? h("span", null, h("strong", null, g.error_code), h("span", { class: "secondaire" }, g.error_detail || "")) : "—",
              h("span", { class: "mono" }, g.utilisateur || "—"),
            ],
            action: () => ouvrirTrace(g.job_id),
          })),
          { numeriques: [3, 4] }
        )
      );
    } catch (erreur) {
      remplacer(zones.generations, UI.blocErreur(erreur));
    }
  }

  async function ouvrirTrace(id) {
    let trace;
    try {
      trace = await Api.get(`${API}/generations/${id}`);
    } catch (erreur) {
      UI.toast(UI.messageErreur(erreur), "erreur");
      return;
    }
    const job = trace.job;
    const corps = [
      h(
        "dl",
        { class: "liste-cles" },
        h("dt", null, "Statut"),
        h("dd", null, UI.badgeStatut(job.status), job.error_code ? ` ${job.error_code} — ${job.error_detail || ""}` : ""),
        h("dt", null, "Lancée · durée"),
        h("dd", null, `${UI.date(job.created_at)} · ${UI.duree(job.duree_ms)}`),
        h("dt", null, "Paramètres"),
        h("dd", null, h("code", null, JSON.stringify(job.request_params))),
        trace.run && [
          h("dt", null, "Poids · graine"),
          h("dd", null, `${trace.run.scoring_version} · ${trace.run.random_seed}`),
          h("dt", null, "LLM"),
          h("dd", null, `${trace.run.llm_call_count} appel(s)${trace.run.used_fallback ? " · texte de repli" : ""} · ${UI.nombre(trace.run.input_tokens + trace.run.output_tokens)} tokens`),
        ]
      ),
    ];

    if (trace.candidats.length) {
      corps.push(
        h("h3", { class: "carte__titre" }, "Plats retenus et décomposition du score"),
        Graphiques.legendeScore(LIBELLES_SCORE),
        UI.tableau(
          ["Jour", "Créneau", "Plat", "Score", "Décomposition"],
          trace.candidats.map((c) => ({
            cellules: [String(c.jour + 1), c.slot, c.plat || "—", UI.nombre(c.score, 3), Graphiques.empilee(Object.fromEntries(Object.entries(c.detail).map(([k, v]) => [k, Number(v)])), { libelles: LIBELLES_SCORE })],
          })),
          { numeriques: [0, 3] }
        )
      );
    } else if (!trace.plan) {
      corps.push(UI.bandeau("info", "Aucun programme matérialisé", "La génération s'est arrêtée avant la composition : le motif d'échec ci-dessus l'explique."));
    }

    if (trace.validations.length) {
      corps.push(
        h("h3", { class: "carte__titre" }, "Validation finale (FN-022)"),
        UI.tableau(
          ["Phase", "Issue", "Contrôles en échec", "Incident"],
          trace.validations.map((v) => ({
            cellules: [v.phase, v.outcome, v.failed_checks.length ? v.failed_checks.join(", ") : "aucun", v.incident ? UI.badge("critique", "Incident") : UI.badge("ok", "Non")],
          }))
        )
      );
    }

    if (trace.journal.length) {
      corps.push(
        h("h3", { class: "carte__titre" }, "Journal d'audit"),
        UI.tableau(
          ["Quand", "Action", "Résultat"],
          trace.journal.map((a) => ({ cellules: [UI.date(a.at), h("span", { class: "mono" }, a.action), a.resultat] }))
        )
      );
    }

    UI.panneau(`Génération ${id.slice(0, 8)}`, corps);
  }

  UI.segmente(document.getElementById("periode"), (v) => {
    periode = Number(v);
    charger();
  });
  document.getElementById("actualiser").addEventListener("click", charger);
  charger();
})();
