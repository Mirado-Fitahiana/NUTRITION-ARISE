/* Laboratoire — la chaîne de recommandation pas à pas (plan §8).
 * Simulation, comparaison de deux variantes, campagne d'évaluation. */
(function () {
  "use strict";

  const { h, icone, remplacer } = UI;
  const API = "/api/v1/admin/lab";
  const zones = {
    formulaire: document.getElementById("formulaire"),
    resultats: document.getElementById("resultats"),
    comparaison: document.getElementById("comparaison"),
    campagne: document.getElementById("campagne"),
  };

  const OBJECTIFS = {
    weight_loss: "Perte de poids",
    weight_maintenance: "Maintien du poids",
    weight_gain: "Prise de poids",
    muscle_gain: "Prise de muscle",
    balanced_diet: "Alimentation équilibrée",
    habit_improvement: "Meilleures habitudes",
  };
  const ALLERGENES = {
    peanut: "Arachide",
    tree_nuts: "Fruits à coque",
    milk: "Lait",
    egg: "Œuf",
    fish: "Poisson",
    crustaceans: "Crustacés",
    molluscs: "Mollusques",
    soy: "Soja",
    gluten: "Gluten",
    sesame: "Sésame",
    mustard: "Moutarde",
    celery: "Céleri",
    sulphites: "Sulfites",
    lupin: "Lupin",
  };
  const RESTRICTIONS = {
    vegetarian: "Végétarien",
    vegan: "Végan",
    no_pork: "Sans porc",
    no_alcohol: "Sans alcool",
    lactose_free: "Sans lactose",
    gluten_free: "Sans gluten",
    religious: "Religieuse",
    forbidden_food: "Aliment interdit",
    custom: "Personnalisée",
  };
  const SEXES = { female: "Femme", male: "Homme", unspecified: "Non précisé" };
  const ACTIVITES = {
    sedentary: "Sédentaire",
    lightly_active: "Peu actif",
    moderately_active: "Modérément actif",
    very_active: "Très actif",
    extremely_active: "Extrêmement actif",
  };
  const CRENEAUX = ["breakfast", "morning_snack", "lunch", "afternoon_snack", "dinner"];
  const LIBELLES_CRENEAUX = {
    breakfast: "Petit-déjeuner",
    morning_snack: "Collation du matin",
    lunch: "Déjeuner",
    afternoon_snack: "Collation de l'après-midi",
    dinner: "Dîner",
  };
  const CONSEILS_ECHEC = {
    CATALOG_TOO_SMALL: "Le vivier compatible est sous le minimum : le moteur refuse plutôt que de servir un programme répétitif. Pour l'essai, abaisser « Vivier minimal » dans les surcharges ; pour le produit, étoffer le catalogue (sprint 03).",
    NO_COMPATIBLE_DISH: "Les contraintes excluent tous les plats. Assouplir une préférence — jamais une allergie.",
    PERIOD_INVALID: "Réduire la durée, ou relever « Durée maximale » dans les surcharges.",
  };

  let options = null;
  let champs = {};
  let derniere = null;
  let jourAffiche = 0;

  // --------------------------------------------------------------------------
  // Formulaire
  // --------------------------------------------------------------------------

  function choixMultiples(nom, valeurs, libelles) {
    return h(
      "div",
      { class: "choix", role: "group" },
      valeurs.map((valeur) => h("label", { class: "choix__option" }, h("input", { type: "checkbox", name: nom, value: valeur }), h("span", null, libelles[valeur] || valeur)))
    );
  }

  function selection(valeurs, libelles, defaut) {
    const element = h("select", { class: "champ__saisie" }, valeurs.map((v) => h("option", { value: v }, libelles[v] || v)));
    element.value = defaut;
    return element;
  }

  function construireFormulaire() {
    const e = options.enums;
    const listeIngredients = h("datalist", { id: "ingredients-fictifs" }, options.catalogue_fictif.ingredients.map((i) => h("option", { value: i })));

    champs = {
      catalogue: "fictif",
      goal: selection(e.goals, OBJECTIFS, "balanced_diet"),
      modeCible: "saisie",
      kcal: h("input", { type: "number", class: "champ__saisie", min: 800, max: 6000, step: 50, value: 2000 }),
      poids: h("input", { type: "number", class: "champ__saisie", min: 25, max: 350, step: 0.5, value: 68 }),
      taille: h("input", { type: "number", class: "champ__saisie", min: 90, max: 250, step: 1, value: 165 }),
      naissance: h("input", { type: "date", class: "champ__saisie", value: "1994-03-12" }),
      sexe: selection(e.sexes, SEXES, "female"),
      activite: selection(e.activity_levels, ACTIVITES, "lightly_active"),
      allergenes: choixMultiples("allergens", e.allergens, ALLERGENES),
      restrictions: choixMultiples("restrictions", e.restrictions, RESTRICTIONS),
      refuses: h("input", { type: "text", class: "champ__saisie", placeholder: "ex. arachide, porc", list: "ingredients-fictifs" }),
      preferes: h("input", { type: "text", class: "champ__saisie", placeholder: "ex. riz-blanc", list: "ingredients-fictifs" }),
      favoris: h("input", { type: "text", class: "champ__saisie", placeholder: "ex. lait-coco", list: "ingredients-fictifs" }),
      jours: h("input", { type: "number", class: "champ__saisie", min: 1, max: 21, step: 1, value: 3 }),
      graine: h("input", { type: "number", class: "champ__saisie", min: 0, step: 1, value: 42 }),
      budget: h("input", { type: "number", class: "champ__saisie", min: 0, step: 500, placeholder: "facultatif" }),
      surcharges: {},
      brouillon: h("input", { type: "checkbox", class: "interrupteur" }),
      curseurs: {},
      reponse: h("textarea", { class: "champ__saisie champ__saisie--mono", rows: 7, spellcheck: "false", placeholder: '{ "plan_summary": "…", "days": [ … ] }' }),
    };

    const zoneSaisie = h("div", { class: "pile" }, UI.champ("Cible calorique (kcal/jour)", champs.kcal));
    const zonePhysio = h(
      "div",
      { class: "pile", hidden: true },
      h("div", { class: "grille grille--2" }, UI.champ("Poids (kg)", champs.poids), UI.champ("Taille (cm)", champs.taille)),
      UI.champ("Date de naissance", champs.naissance),
      h("div", { class: "grille grille--2" }, UI.champ("Sexe", champs.sexe), UI.champ("Activité", champs.activite)),
      h("p", { class: "aide" }, "Cible calculée par Mifflin-St Jeor, planchers de sécurité compris. Ces valeurs ne doivent jamais atteindre le modèle : l'étape ⑤ le vérifie.")
    );

    const segmentCatalogue = h(
      "div",
      { class: "segmente", role: "group", "aria-label": "Catalogue" },
      h("button", { type: "button", class: "segmente__option est-actif", "data-valeur": "fictif" }, "Fictif"),
      h("button", { type: "button", class: "segmente__option", "data-valeur": "reel" }, "Publié en base")
    );
    UI.segmente(segmentCatalogue, (v) => (champs.catalogue = v));

    const segmentCible = h(
      "div",
      { class: "segmente", role: "group", "aria-label": "Cible calorique" },
      h("button", { type: "button", class: "segmente__option est-actif", "data-valeur": "saisie" }, "Saisie"),
      h("button", { type: "button", class: "segmente__option", "data-valeur": "physio" }, "Calculée")
    );
    UI.segmente(segmentCible, (v) => {
      champs.modeCible = v;
      zoneSaisie.hidden = v !== "saisie";
      zonePhysio.hidden = v !== "physio";
    });

    const surcharges = options.surchargeables.map((r) => {
      const saisie = h("input", { type: "number", class: "champ__saisie", min: r.minimum, max: r.maximum, step: r.type === "entier" ? 1 : 0.01, placeholder: `effectif : ${r.effectif}` });
      champs.surcharges[r.cle] = { saisie: saisie, type: r.type };
      return UI.champ(r.libelle, saisie);
    });

    const curseurs = Object.entries(options.poids.libelles).map(([cle, libelle]) => {
      const valeur = Number(options.poids.valeurs[cle] || 0);
      const curseur = h("input", { type: "range", min: 0, max: Number(options.poids.maximum), step: 0.05, value: valeur, "aria-label": libelle });
      const sortie = h("output", null, valeur.toFixed(2));
      curseur.addEventListener("input", () => {
        sortie.textContent = Number(curseur.value).toFixed(2);
        champs.brouillon.checked = true;
      });
      champs.curseurs[cle] = curseur;
      return [h("span", null, h("i", { style: { display: "inline-block", width: "10px", height: "10px", borderRadius: "3px", marginRight: "6px", background: Graphiques.COULEURS_SCORE[cle] } }), libelle), curseur, sortie];
    });

    const boutonSimuler = h("button", { type: "button", class: "bouton bouton--primaire", id: "simuler" }, icone("play"), "Lancer la simulation");
    boutonSimuler.addEventListener("click", simuler);
    const boutonComparer = h("button", { type: "button", class: "bouton bouton--contour" }, "Comparer");
    boutonComparer.addEventListener("click", comparer);

    remplacer(
      zones.formulaire,
      listeIngredients,
      h("header", { class: "carte__tete" }, h("p", { class: "surtitre" }, "Profil d'essai"), h("h2", { class: "carte__titre" }, "Qui mange ?"), h("p", { class: "carte__sous-titre" }, `Version des poids active : ${options.poids.version} · réglages : ${options.regles.source}`)),
      UI.champ("Catalogue", segmentCatalogue, `Fictif : ${options.catalogue_fictif.total} plats inventés. Publié : ${options.plats_publies === null ? "base injoignable" : options.plats_publies + " plat(s)"}.`),
      UI.champ("Objectif", champs.goal),
      UI.champ("Cible calorique", segmentCible),
      zoneSaisie,
      zonePhysio,
      UI.champ("Allergies (exclusion dure)", champs.allergenes),
      UI.champ("Restrictions (exclusion dure)", champs.restrictions),
      UI.champ("Ingrédients refusés", champs.refuses, "Slugs séparés par des virgules — exclusion dure."),
      h("div", { class: "grille grille--2" }, UI.champ("Préférés", champs.preferes), UI.champ("Favoris", champs.favoris)),
      h(
        "div",
        { class: "grille grille--2" },
        UI.champ("Jours", champs.jours),
        UI.champ(
          "Graine",
          h(
            "div",
            { class: "rangee", style: { flexWrap: "nowrap" } },
            champs.graine,
            h("button", { type: "button", class: "bouton bouton--fantome bouton--petit", title: "Graine au hasard", onclick: () => (champs.graine.value = Math.floor(Math.random() * 100000)) }, icone("de"))
          )
        )
      ),
      UI.champ("Budget par jour (Ar)", champs.budget),
      h("details", { class: "repli" }, h("summary", null, icone("reglages"), "Surcharges de réglages"), h("div", { class: "pile" }, h("p", { class: "aide" }, "Validées par le même registre que l'API. Vide : la valeur effective s'applique."), surcharges)),
      h(
        "details",
        { class: "repli" },
        h("summary", null, icone("tendance"), "Poids du score (brouillon)"),
        h("div", { class: "pile" }, h("label", { class: "interrupteur-champ" }, champs.brouillon, "Utiliser ce brouillon plutôt que le jeu actif"), h("div", { class: "poids-saisie" }, curseurs), h("p", { class: "aide" }, "Les poids pondèrent chaque composante (0 à 1) : seul leur rapport change le classement."))
      ),
      h(
        "details",
        { class: "repli" },
        h("summary", null, icone("console"), "Réponse LLM simulée"),
        h(
          "div",
          { class: "pile" },
          h("p", { class: "aide" }, options.llm.motif),
          champs.reponse,
          h(
            "div",
            { class: "rangee" },
            h("button", { type: "button", class: "bouton bouton--fantome bouton--mini", onclick: () => exempleReponse(true) }, "Exemple conforme"),
            h("button", { type: "button", class: "bouton bouton--fantome bouton--mini", onclick: () => exempleReponse(false) }, "Exemple fautif"),
            h("button", { type: "button", class: "bouton bouton--fantome bouton--mini", onclick: () => (champs.reponse.value = "") }, "Vider")
          )
        )
      ),
      h("div", { class: "labo__actions" }, boutonSimuler, boutonComparer)
    );
  }

  function cochees(nom) {
    return Array.from(zones.formulaire.querySelectorAll(`input[name="${nom}"]:checked`)).map((i) => i.value);
  }

  function liste(texte) {
    return texte.split(",").map((t) => t.trim()).filter(Boolean);
  }

  function lirePoids() {
    return Object.fromEntries(Object.entries(champs.curseurs).map(([cle, curseur]) => [cle, Number(curseur.value).toFixed(2)]));
  }

  function lireFormulaire() {
    const profil = {
      goal: champs.goal.value,
      allergens: cochees("allergens"),
      restrictions: cochees("restrictions"),
      disliked_ingredients: liste(champs.refuses.value),
      preferred_ingredients: liste(champs.preferes.value),
      favorite_ingredients: liste(champs.favoris.value),
    };
    if (champs.modeCible === "physio") {
      profil.physio = { weight_kg: champs.poids.value, height_cm: champs.taille.value, birth_date: champs.naissance.value, sex: champs.sexe.value, activity_level: champs.activite.value };
    } else {
      profil.kcal_target = champs.kcal.value;
    }
    if (champs.budget.value) profil.budget_par_jour = champs.budget.value;

    const demande = { profil: profil, jours: Number(champs.jours.value), seed: Number(champs.graine.value), catalogue: champs.catalogue };
    const surcharges = {};
    Object.entries(champs.surcharges).forEach(([cle, { saisie }]) => {
      if (saisie.value !== "") surcharges[cle] = saisie.value;
    });
    if (Object.keys(surcharges).length) demande.regles = surcharges;
    if (champs.brouillon.checked) demande.poids = lirePoids();

    const texte = champs.reponse.value.trim();
    if (texte) {
      try {
        demande.reponse_llm = JSON.parse(texte);
      } catch (erreur) {
        throw new Error("La réponse simulée n'est pas un JSON valide : " + erreur.message);
      }
    }
    return demande;
  }

  function exempleReponse(conforme) {
    if (!derniere || !derniere.contexte) {
      UI.toast("Lancer d'abord une simulation réussie : l'exemple reprend ses identifiants de repas.", "alerte");
      return;
    }
    const jours = derniere.contexte.payload.jours.map((jour) => ({
      day: jour.jour,
      meals: jour.repas.map((repas) => ({ meal_id: repas.meal_id, justification: `${repas.plat} : ${repas.motifs_selection[0] || "un choix du moteur"}.` })),
      tip: "Buvez de l'eau régulièrement.",
    }));
    if (!conforme) {
      jours[0].meals.push({ meal_id: "J9-dinner", justification: "Un plat que le moteur n'a jamais choisi." });
      jours[0].meals[0].justification = "Environ 650 kcal, pile ce qu'il faut.";
    }
    champs.reponse.value = JSON.stringify({ plan_summary: "Un programme construit autour de plats simples et variés.", days: jours, warnings: [] }, null, 2);
  }

  // --------------------------------------------------------------------------
  // Étapes
  // --------------------------------------------------------------------------

  function etape(numero, titre, sousTitre, contenu, sansObjet) {
    return h(
      "section",
      { class: "carte etape" + (sansObjet ? " etape--sans-objet" : "") },
      h("span", { class: "etape__numero", "aria-hidden": "true" }, String(numero)),
      h(
        "div",
        { class: "etape__corps" },
        h("header", { class: "carte__tete", style: { marginBottom: "0" } }, h("h2", { class: "carte__titre" }, titre), h("p", { class: "carte__sous-titre" }, sousTitre)),
        sansObjet ? h("p", { class: "aide" }, "Sans objet : la composition a échoué, il n'y a pas de programme à traiter.") : contenu
      )
    );
  }

  function bandeauCatalogue(s) {
    const info = s.catalogue;
    const puces = h(
      "div",
      { class: "rangee" },
      h("span", { class: "puce" }, icone("tendance"), `Poids : ${s.poids.version}`),
      h("span", { class: "puce" }, icone("reglages"), `Réglages : ${s.regles.source}`),
      h("span", { class: "puce" }, icone("de"), `Graine ${s.seed}`),
      h("span", { class: "puce" }, icone("history"), `${UI.nombre(s.duree_ms, 1)} ms`)
    );
    if (info.fictif) {
      return h("div", { class: "pile" }, UI.bandeau("fictif", `Catalogue FICTIF — ${info.total} plats inventés`, info.avertissement || "Données ni sourcées ni validées."), puces);
    }
    const cible = options.cible_plats_publies;
    return h(
      "div",
      { class: "pile" },
      info.total < cible
        ? UI.bandeau("alerte", `${info.total} plat(s) publié(s) — résultats non représentatifs sous ${cible}`, "Le laboratoire montre ce que le moteur ferait aujourd'hui, pas la qualité du produit à venir.")
        : UI.bandeau("info", `Catalogue publié : ${info.total} plats`, null),
      puces
    );
  }

  function resumeProfil(s) {
    const p = s.profil;
    const cartes = [UI.carteStat("Cible calorique", `${UI.nombre(p.kcal)} kcal`, "aubergine", p.origine === "calculee" ? "calculée depuis le profil" : "saisie")];
    if (p.besoins) {
      cartes.push(
        UI.carteStat("Métabolisme de base", `${UI.nombre(p.besoins.bmr)} kcal`, "aubergine", p.besoins.formule),
        UI.carteStat("Dépense totale", `${UI.nombre(p.besoins.tdee)} kcal`, "aubergine", `P ${p.besoins.proteines_g} g · G ${p.besoins.glucides_g} g · L ${p.besoins.lipides_g} g`)
      );
    }
    if (p.plancher_applique) cartes.push(UI.carteStat("Plancher de sécurité", "Appliqué", "alerte", "la cible a été relevée"));
    return h("div", { class: "grille grille--4" }, cartes);
  }

  function vivier(s) {
    const v = s.vivier;
    return [
      Graphiques.entonnoir([
        { libelle: "Catalogue", valeur: v.total },
        { libelle: "Après filtrage dur", valeur: v.retenus },
        { libelle: "Vivier minimal requis", valeur: v.min_pool_size, seuil: true },
      ]),
      v.par_motif.length ? h("div", null, h("p", { class: "etiquette" }, "Exclusions par motif"), Graphiques.repartition(v.par_motif, { format: (n) => UI.nombre(n), couleur: Graphiques.COULEURS.aubergine })) : h("p", { class: "aide" }, "Aucun plat exclu par le filtrage dur."),
      v.exclus.length &&
        h(
          "details",
          { class: "repli" },
          h("summary", null, `Voir les ${v.exclus.length} plat(s) exclu(s)`),
          UI.tableau(["Plat", "Motif"], v.exclus.map((e) => ({ cellules: [h("span", null, e.nom, h("span", { class: "secondaire mono" }, e.slug)), e.motif] })))
        ),
    ];
  }

  function score(s) {
    const creneaux = s.score.creneaux;
    if (!creneaux.length) {
      return UI.vide("Aucun créneau noté", "La composition s'est arrêtée avant de noter un candidat : voir l'étape ③.", "info");
    }
    const jours = Array.from(new Set(creneaux.map((c) => c.jour))).sort((a, b) => a - b);
    if (!jours.includes(jourAffiche)) jourAffiche = jours[0];
    const zone = h("div", { class: "pile" });
    const onglets = h(
      "div",
      { class: "segmente", role: "group", "aria-label": "Jour" },
      jours.map((j) => h("button", { type: "button", class: "segmente__option" + (j === jourAffiche ? " est-actif" : ""), "data-valeur": String(j) }, `Jour ${j + 1}`))
    );

    function afficher() {
      remplacer(
        zone,
        creneaux
          .filter((c) => c.jour === jourAffiche)
          .map((c) =>
            h(
              "div",
              { class: "creneau" },
              h("div", { class: "rangee rangee--ecart" }, h("strong", null, c.creneau), h("span", { class: "aide" }, `cible ${UI.nombre(c.kcal_cible)} kcal · ${c.nb_candidats} candidat(s) noté(s)`)),
              c.candidats.length
                ? UI.tableau(
                    ["Rang", "Plat", "kcal", "Décomposition", "Score"],
                    c.candidats.map((cand) => ({
                      cellules: [String(cand.rang), h("span", null, cand.nom, cand.retenu && [" ", UI.badge("ok", "Retenu")]), UI.nombre(cand.kcal), Graphiques.empilee(cand.detail, { libelles: options.poids.libelles }), h("strong", null, UI.nombre(cand.total, 3))],
                      classe: cand.retenu ? "est-mis-en-avant" : null,
                    })),
                    { numeriques: [0, 2, 4] }
                  )
                : UI.bandeau("erreur", "Créneau non couvert", "Aucun plat éligible n'a survécu aux règles de variété."),
              c.ecartes.length && h("p", { class: "aide" }, h("strong", null, "Écartés par les règles de variété : "), c.ecartes.map((e) => `${e.nom} (${e.motif})`).join(" · "))
            )
          )
      );
    }

    UI.segmente(onglets, (v) => {
      jourAffiche = Number(v);
      afficher();
    });
    afficher();
    return [h("div", { class: "rangee rangee--ecart" }, onglets, Graphiques.legendeScore(options.poids.libelles)), zone];
  }

  function composition(s) {
    const c = s.composition;
    if (c.statut !== "ok") {
      return UI.bandeau("erreur", `Refus explicite : ${c.echec.code}`, [h("p", null, c.echec.message), h("p", null, CONSEILS_ECHEC[c.echec.code] || "")], Object.keys(c.echec.details || {}).length ? JSON.stringify(c.echec.details) : null);
    }
    const colonnes = c.jours.length;
    const grille = h("div", { class: "calendrier", style: { gridTemplateColumns: `140px repeat(${colonnes}, minmax(150px, 1fr))` } });
    grille.appendChild(h("span"));
    c.jours.forEach((j) => grille.appendChild(h("span", { class: "calendrier__entete" }, `Jour ${j.index + 1}`)));
    CRENEAUX.forEach((creneau) => {
      grille.appendChild(h("span", { class: "calendrier__entete", style: { alignSelf: "center" } }, LIBELLES_CRENEAUX[creneau]));
      c.jours.forEach((j) => {
        const repas = j.repas.find((r) => r.slot === creneau);
        grille.appendChild(
          repas
            ? h("div", { class: "calendrier__case", title: repas.allergenes.length ? "Allergènes : " + repas.allergenes.join(", ") : "Aucun allergène" }, h("span", { class: "calendrier__plat" }, repas.nom), h("span", { class: "calendrier__meta" }, `${UI.nombre(repas.kcal)} / ${UI.nombre(repas.kcal_cible)} kcal · score ${UI.nombre(repas.score, 3)}`))
            : h("div", { class: "calendrier__case calendrier__case--vide" }, "Non couvert")
        );
      });
    });
    grille.appendChild(h("span", { class: "calendrier__entete", style: { alignSelf: "center" } }, "Total"));
    c.jours.forEach((j) =>
      grille.appendChild(h("div", { class: "calendrier__total" + (j.dans_tolerance ? "" : " calendrier__total--hors") }, `${UI.nombre(j.kcal_total)} kcal · ${j.ecart >= 0 ? "+" : ""}${UI.pourcentage(j.ecart, 1)}`, h("br"), j.dans_tolerance ? "dans la tolérance" : `hors tolérance (±${UI.pourcentage(c.tolerance_jour)})`))
    );
    return [
      c.non_couverts.length ? UI.bandeau("alerte", `${c.non_couverts.length} créneau(x) non couvert(s)`, c.non_couverts.map((n) => `Jour ${n.jour + 1} · ${n.creneau}`).join(", ")) : null,
      h("div", { class: "defilement", style: { padding: "12px" } }, grille),
    ];
  }

  function validation(v) {
    return [
      v.incident_securite ? UI.bandeau("erreur", "Incident de sécurité alimentaire", "Un contrôle de sécurité (1 à 4) a échoué : en production, ce programme ne serait pas délivré.") : null,
      h(
        "ul",
        { class: "controles" },
        v.controles.map((c) =>
          h(
            "li",
            { class: "controle" + (c.statut === "echec" ? " controle--echec" : "") },
            icone(c.statut === "echec" ? "error" : "check"),
            h("span", { class: "controle__libelle" }, `${c.numero}. ${c.libelle}`),
            c.securite && UI.badge("rose", "Sécurité"),
            UI.badge(c.statut === "echec" ? "critique" : "ok", c.statut === "echec" ? "Échec" : "Conforme")
          )
        )
      ),
      h("p", { class: "aide" }, v.non_implementes),
    ];
  }

  function contexte(s) {
    const c = s.contexte;
    const texte = JSON.stringify(c.payload, null, 2);
    return [
      c.violations.length
        ? UI.bandeau("erreur", "Données interdites dans le contexte (FN-021)", c.violations.map((v) => h("p", null, v)))
        : h("div", { class: "bandeau bandeau--succes" }, icone("shield"), h("div", { class: "bandeau__texte" }, h("p", { class: "bandeau__titre" }, "Aucune donnée interdite"), h("p", null, "Ni identité, ni localisation, ni donnée de santé : seulement l'objectif, la cible agrégée, les types de contraintes et les plats déjà retenus."))),
      h(
        "div",
        { class: "rangee rangee--ecart" },
        h("span", { class: "aide" }, `${UI.octets(c.octets)} · ${c.payload.jours.reduce((t, j) => t + j.repas.length, 0)} repas · version ${c.payload.version_prompt}`),
        h("button", { type: "button", class: "bouton bouton--fantome bouton--mini", onclick: () => UI.copier(texte) }, icone("copy"), "Copier le JSON")
      ),
      h("details", { class: "repli" }, h("summary", null, "Voir le contexte transmis"), h("pre", { class: "json" }, texte)),
    ];
  }

  function redaction(s) {
    const r = s.redaction;
    const blocs = [
      h("div", { class: "rangee" }, UI.badge(r.mode === "repli" ? "info" : "rose", r.mode === "repli" ? "Texte de repli" : "Réponse simulée"), r.repli_declenche && UI.badge("alerte", "Réponse rejetée → repli")),
      h("blockquote", { class: "citation" }, r.repli_declenche ? s.redaction.texte : r.texte),
    ];
    if (r.verification) {
      const v = r.verification;
      blocs.push(
        v.valide
          ? UI.bandeau("succes", "Réponse acceptée", `${v.meal_ids_couverts}/${v.meal_ids_attendus} repas justifiés.${v.avertissements.length ? " " + v.avertissements.join(" ") : ""}`)
          : UI.bandeau("erreur", "Réponse rejetée (FN-021)", [v.erreurs.map((e) => h("p", null, e)), v.extraits_avec_chiffres.length ? h("p", null, "Extraits : ", v.extraits_avec_chiffres.map((x) => `« ${x} »`).join(" · ")) : null])
      );
    } else {
      blocs.push(h("p", { class: "aide" }, r.llm.motif));
    }
    return blocs;
  }

  function apres(s) {
    const a = s.apres_llm;
    return [
      h(
        "div",
        { class: "preuve-d06" },
        icone(a.selection_inchangee ? "shield" : "error"),
        h("div", null, h("strong", null, a.selection_inchangee ? "Sélection inchangée" : "Sélection modifiée"), h("span", null, a.explication))
      ),
      h("div", { class: "rangee" }, UI.badge(a.validation.conforme ? "ok" : "alerte", a.validation.conforme ? "Revalidé : conforme" : "Revalidé : non conforme"), UI.badge(a.validation.incident_securite ? "critique" : "ok", a.validation.incident_securite ? "Incident de sécurité" : "Aucun incident de sécurité")),
    ];
  }

  function rendreSimulation(s) {
    const echec = s.composition.statut !== "ok";
    remplacer(
      zones.resultats,
      bandeauCatalogue(s),
      resumeProfil(s),
      etape(1, "Vivier", "Filtrage dur (FN-019) : ce qui est interdit est écarté avant tout calcul de score.", vivier(s)),
      etape(2, "Score", "Score métier (FN-020) : c'est lui, et lui seul, qui décide du plat retenu.", score(s)),
      etape(3, "Composition", "Programme jour par jour (FN-023, FN-024) — ou un refus motivé.", composition(s)),
      etape(4, "Validation", "Contrôles rejoués sur le programme final (FN-022).", echec ? null : validation(s.validation), echec),
      etape(5, "Contexte transmis au modèle", "Ce que le LLM recevrait : un programme déjà décidé (FN-021).", echec ? null : contexte(s), echec),
      etape(6, "Rédaction", "Le seul rôle du modèle : rédiger, en un appel (D-07).", echec ? null : redaction(s), echec),
      etape(7, "Après rédaction", "La preuve de D-06 : le texte n'a pas pu modifier la sélection.", echec ? null : apres(s), echec)
    );
  }

  // --------------------------------------------------------------------------
  // Actions
  // --------------------------------------------------------------------------

  async function simuler() {
    let demande;
    try {
      demande = lireFormulaire();
    } catch (erreur) {
      UI.toast(erreur.message, "erreur", 8000);
      return;
    }
    const bouton = document.getElementById("simuler");
    UI.occuper(bouton, true);
    remplacer(zones.comparaison);
    remplacer(zones.resultats, h("div", { class: "carte" }, UI.chargement(8)));
    try {
      derniere = await Api.post(`${API}/simulate`, demande);
      rendreSimulation(derniere);
    } catch (erreur) {
      remplacer(zones.resultats, UI.blocErreur(erreur));
    } finally {
      UI.occuper(bouton, false);
    }
  }

  async function comparer() {
    let demande;
    try {
      demande = lireFormulaire();
    } catch (erreur) {
      UI.toast(erreur.message, "erreur", 8000);
      return;
    }
    const graineB = h("input", { type: "number", class: "champ__saisie", min: 0, value: Number(champs.graine.value) + 1 });
    const parPoids = h("input", { type: "checkbox", class: "interrupteur" });
    parPoids.checked = champs.brouillon.checked;
    const ok = await UI.confirmer({
      titre: "Comparer deux variantes",
      libelle: "Comparer",
      corps: [
        h("p", null, "Même profil, même catalogue. La variante change la graine — ou oppose le brouillon de poids au jeu actif."),
        UI.champ("Graine de la variante", graineB),
        h("label", { class: "interrupteur-champ" }, parPoids, "Variante = brouillon de poids (la base garde le jeu actif)"),
      ],
    });
    if (!ok) return;

    const corps = { base: Object.assign({}, demande) };
    if (parPoids.checked) {
      delete corps.base.poids;
      corps.variante_poids = lirePoids();
    } else {
      corps.variante_seed = Number(graineB.value);
    }

    remplacer(zones.comparaison, h("div", { class: "carte" }, UI.chargement(5)));
    try {
      const r = await Api.post(`${API}/compare`, corps);
      const c = r.comparaison;
      const libelleB = parPoids.checked ? "Variante (brouillon)" : `Variante (graine ${corps.variante_seed})`;
      remplacer(
        zones.comparaison,
        h(
          "section",
          { class: "carte" },
          h(
            "header",
            { class: "carte__tete carte__tete--rangee" },
            h("div", null, h("p", { class: "surtitre" }, "Comparaison"), h("h2", { class: "carte__titre" }, `${c.changements} créneau(x) changent sur ${c.creneaux}`), h("p", { class: "carte__sous-titre" }, `Base : ${r.a.poids.version}, graine ${r.a.seed} — ${libelleB} : ${r.b.poids.version}, graine ${r.b.seed}`)),
            h("button", { type: "button", class: "bouton bouton--fantome bouton--petit", onclick: () => remplacer(zones.comparaison) }, icone("close"), "Fermer")
          ),
          !parPoids.checked && c.changements === 0 && c.statut_a === "ok"
            ? UI.bandeau(
                "info",
                "La graine ne change aucun plat",
                "Mesuré sur cette comparaison : composer() mélange le vivier avec la graine, puis classe les candidats par score et par slug — le tirage n'influe donc pas sur la sélection. Pour voir un effet, comparer deux jeux de poids."
              )
            : null,
          c.statut_a !== "ok" || c.statut_b !== "ok"
            ? UI.bandeau("alerte", "Une des variantes n'a pas produit de programme", `Base : ${c.statut_a} · variante : ${c.statut_b}`)
            : UI.tableau(
                ["Jour", "Créneau", "Base", "Variante"],
                c.lignes.map((l) => ({
                  cellules: [String(l.jour + 1), l.creneau, l.a ? `${l.a.nom} · ${UI.nombre(l.a.score, 3)}` : "—", h("span", null, l.b ? `${l.b.nom} · ${UI.nombre(l.b.score, 3)}` : "—", l.change && [" ", UI.badge("rose", "Change")])],
                  classe: l.change ? "est-mis-en-avant" : null,
                })),
                { numeriques: [0] }
              )
        )
      );
    } catch (erreur) {
      remplacer(zones.comparaison, UI.blocErreur(erreur));
    }
  }

  function rendreCampagneInitiale() {
    const bouton = h("button", { type: "button", class: "bouton bouton--secondaire" }, icone("play"), "Lancer la campagne");
    bouton.addEventListener("click", () => campagne(bouton));
    remplacer(
      zones.campagne,
      h(
        "header",
        { class: "carte__tete carte__tete--rangee" },
        h("div", null, h("p", { class: "surtitre" }, "Résultats testables"), h("h2", { class: "carte__titre" }, "Cas d'évaluation"), h("p", { class: "carte__sous-titre" }, `${options.cas.length} cas versionnés dans evaluation/cas — les mêmes que rejoue pytest à chaque commit.`)),
        bouton
      ),
      h("div", { id: "campagne-resultats" }, UI.tableau(["Cas", "Fichier"], options.cas.map((c) => ({ cellules: [h("span", null, c.titre, c.description && h("span", { class: "secondaire" }, c.description)), h("span", { class: "mono" }, c.fichier)] }))))
    );
  }

  async function campagne(bouton) {
    const zone = document.getElementById("campagne-resultats");
    UI.occuper(bouton, true);
    try {
      const r = await Api.post(`${API}/evaluations`);
      remplacer(
        zone,
        h(
          "div",
          { class: "pile" },
          h(
            "div",
            { class: "verdict" },
            Graphiques.anneau(r.total ? r.reussis / r.total : null, { texte: `${r.reussis}/${r.total}`, couleur: r.echoues ? Graphiques.COULEURS.rose : Graphiques.COULEURS.vert, libelle: "Cas réussis" }),
            h("div", { class: "verdict__texte" }, h("h3", { class: "verdict__titre" }, r.echoues ? `${r.echoues} cas en échec` : "Tous les cas passent"), h("p", { class: "aide" }, `${r.conditions} · ${UI.nombre(r.duree_ms, 1)} ms`))
          ),
          r.cas.map((c) =>
            h(
              "details",
              { class: "repli", open: !c.ok },
              h("summary", null, UI.badge(c.ok ? "ok" : "critique", c.ok ? "Réussi" : "Échec"), " ", c.titre, h("span", { class: "aide", style: { marginLeft: "8px" } }, c.composition)),
              h(
                "ul",
                { class: "cas__assertions" },
                c.assertions.map((a) => h("li", { class: a.ok ? "" : "est-ko" }, icone(a.ok ? "check" : "close"), h("span", null, h("strong", { class: "mono" }, a.nom), " — ", a.detail)))
              )
            )
          )
        )
      );
    } catch (erreur) {
      remplacer(zone, UI.blocErreur(erreur));
    } finally {
      UI.occuper(bouton, false);
    }
  }

  async function initialiser() {
    remplacer(zones.formulaire, UI.chargement(10));
    try {
      options = await Api.get(`${API}/options`);
      construireFormulaire();
      rendreCampagneInitiale();
    } catch (erreur) {
      remplacer(zones.formulaire, UI.blocErreur(erreur));
      remplacer(zones.campagne);
    }
  }

  initialiser();
})();
