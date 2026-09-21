/* Pilotage ARISE — graphiques en SVG dessinés par la page.
 *
 * Aucune bibliothèque externe, comme le banc d'essai. Palette ARISE : rose,
 * aubergine, violet, corail. Chaque graphique porte un libellé accessible et
 * des infobulles (`<title>`) : une barre sans valeur lisible ne prouve rien.
 */
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const { h } = UI;

  const COULEURS = {
    rose: "#E72377",
    aubergine: "#3E0E54",
    violet: "#8B5CF6",
    corail: "#F97861",
    lilas: "#B29FBB",
    auberginClair: "#826B8D",
    bleu: "#3B82F6",
    vert: "#10B981",
  };

  const COULEURS_SCORE = {
    nutrition: COULEURS.rose,
    cost: COULEURS.corail,
    preference: COULEURS.violet,
    variety: COULEURS.auberginClair,
    favorite: COULEURS.bleu,
  };

  function s(balise, attributs) {
    const element = document.createElementNS(NS, balise);
    for (const [cle, valeur] of Object.entries(attributs || {})) {
      if (valeur !== null && valeur !== undefined) element.setAttribute(cle, String(valeur));
    }
    for (const enfant of Array.prototype.slice.call(arguments, 2).flat()) {
      if (enfant === null || enfant === undefined) continue;
      element.appendChild(typeof enfant === "string" ? document.createTextNode(enfant) : enfant);
    }
    return element;
  }

  function legende(series) {
    return h("ul", { class: "legende" }, series.map((serie) => h("li", null, h("i", { style: { background: serie.couleur } }), serie.libelle)));
  }

  /* Barres verticales empilées. `donnees` : [{ x, <cle>: nombre }]. */
  function barres(donnees, options) {
    const series = options.series;
    const hauteur = options.hauteur || 170;
    const formatX = options.formatX || ((x) => x);
    const formatY = options.formatY || ((y) => UI.nombre(y));
    const largeur = Math.max(360, donnees.length * 30 + 50);
    const marge = { haut: 10, bas: 26, gauche: 40, droite: 6 };
    const totaux = donnees.map((d) => series.reduce((t, serie) => t + (Number(d[serie.cle]) || 0), 0));
    const max = Math.max(1, ...totaux);
    const echelle = (hauteur - marge.haut - marge.bas) / max;
    const pas = (largeur - marge.gauche - marge.droite) / Math.max(1, donnees.length);

    const svg = s("svg", { viewBox: `0 0 ${largeur} ${hauteur}`, class: "graphe", role: "img", "aria-label": options.libelle || "Graphique en barres" });
    [0, 0.5, 1].forEach((fraction) => {
      const y = hauteur - marge.bas - fraction * max * echelle;
      svg.appendChild(s("line", { x1: marge.gauche, x2: largeur - marge.droite, y1: y, y2: y, class: "graphe__grille" }));
      svg.appendChild(s("text", { x: marge.gauche - 8, y: y + 4, class: "graphe__axe", "text-anchor": "end" }, formatY(Math.round(fraction * max))));
    });

    const saut = Math.max(1, Math.ceil(donnees.length / 10));
    donnees.forEach((d, i) => {
      const x = marge.gauche + i * pas + pas * 0.2;
      const largeurBarre = Math.max(4, pas * 0.6);
      let base = hauteur - marge.bas;
      series.forEach((serie) => {
        const valeur = Number(d[serie.cle]) || 0;
        if (!valeur) return;
        const hauteurBarre = valeur * echelle;
        base -= hauteurBarre;
        svg.appendChild(s("rect", { x: x, y: base, width: largeurBarre, height: hauteurBarre, rx: 3, fill: serie.couleur }, s("title", null, `${formatX(d.x)} — ${serie.libelle} : ${formatY(valeur)}`)));
      });
      if (i % saut === 0) {
        svg.appendChild(s("text", { x: x + largeurBarre / 2, y: hauteur - 8, class: "graphe__axe", "text-anchor": "middle" }, formatX(d.x)));
      }
    });
    return svg;
  }

  /* Barres horizontales en HTML : lisibles, et qui se replient sur mobile. */
  function repartition(elements, options) {
    const opts = options || {};
    const format = opts.format || ((v) => UI.nombre(v, 2));
    const max = opts.max || Math.max(1, ...elements.map((e) => Number(e.valeur) || 0));
    return h(
      "ul",
      { class: "repartition" },
      elements.map((element, i) =>
        h(
          "li",
          { class: "repartition__ligne" },
          h("span", { class: "repartition__libelle", title: element.libelle }, element.libelle),
          h(
            "span",
            { class: "repartition__piste" },
            h("span", {
              class: "repartition__barre",
              style: {
                width: `${Math.max(2, ((Number(element.valeur) || 0) / max) * 100)}%`,
                background: element.couleur || opts.couleur || [COULEURS.rose, COULEURS.aubergine, COULEURS.violet, COULEURS.corail][i % 4],
              },
            })
          ),
          h("span", { class: "repartition__valeur" }, format(element.valeur))
        )
      )
    );
  }

  /* Anneau de progression. `ratio` nul : piste en pointillés, pas un zéro. */
  function anneau(ratio, options) {
    const opts = options || {};
    const taille = opts.taille || 104;
    const epaisseur = opts.epaisseur || 11;
    const rayon = (taille - epaisseur) / 2;
    const circonference = 2 * Math.PI * rayon;
    const centre = taille / 2;
    const inconnu = ratio === null || ratio === undefined;

    const svg = s("svg", { viewBox: `0 0 ${taille} ${taille}`, width: taille, height: taille, class: "anneau", role: "img", "aria-label": opts.libelle || "Progression" });
    svg.appendChild(s("circle", { cx: centre, cy: centre, r: rayon, fill: "none", "stroke-width": epaisseur, class: "anneau__piste" + (inconnu ? " anneau__piste--inconnu" : "") }));
    if (!inconnu) {
      const valeur = Math.max(0, Math.min(1, Number(ratio)));
      svg.appendChild(
        s("circle", {
          cx: centre,
          cy: centre,
          r: rayon,
          fill: "none",
          stroke: opts.couleur || COULEURS.rose,
          "stroke-width": epaisseur,
          "stroke-linecap": "round",
          "stroke-dasharray": `${valeur * circonference} ${circonference}`,
          transform: `rotate(-90 ${centre} ${centre})`,
        })
      );
    }
    svg.appendChild(s("text", { x: centre, y: centre + 6, "text-anchor": "middle", class: "anneau__texte" }, opts.texte !== undefined ? opts.texte : inconnu ? "—" : UI.pourcentage(ratio)));
    return svg;
  }

  /* Décomposition d'un score FN-020 en segments colorés. */
  function empilee(detail, options) {
    const opts = options || {};
    const max = opts.max || 1;
    const libelles = opts.libelles || {};
    const conteneur = h("div", { class: "empilee", role: "img" });
    const description = [];
    Object.keys(COULEURS_SCORE).forEach((cle) => {
      const valeur = Number(detail && detail[cle]) || 0;
      if (valeur <= 0) return;
      description.push(`${libelles[cle] || cle} ${UI.nombre(valeur, 3)}`);
      conteneur.appendChild(h("span", { class: "empilee__segment", style: { width: `${Math.min(100, (valeur / max) * 100)}%`, background: COULEURS_SCORE[cle] }, title: `${libelles[cle] || cle} : ${UI.nombre(valeur, 3)}` }));
    });
    conteneur.setAttribute("aria-label", description.join(", ") || "score nul");
    return conteneur;
  }

  function legendeScore(libelles) {
    return legende(Object.keys(COULEURS_SCORE).map((cle) => ({ libelle: libelles[cle] || cle, couleur: COULEURS_SCORE[cle] })));
  }

  /* Entonnoir : catalogue → vivier → seuil requis. */
  function entonnoir(etapes) {
    const max = Math.max(1, ...etapes.map((e) => Number(e.valeur) || 0));
    return h(
      "div",
      { class: "entonnoir" },
      etapes.map((etape) =>
        h(
          "div",
          { class: "entonnoir__ligne" + (etape.seuil ? " entonnoir__ligne--seuil" : "") },
          h("span", null, etape.libelle),
          h("span", { class: "entonnoir__barre", style: { width: `${Math.max(3, ((Number(etape.valeur) || 0) / max) * 100)}%` }, title: `${etape.libelle} : ${etape.valeur}` }),
          h("span", { class: "entonnoir__valeur" }, UI.nombre(etape.valeur))
        )
      )
    );
  }

  /* Relevés de structure SPIKE-01 : les relevés retenus pour le critère 3 en rose. */
  function releves(serie) {
    const donnees = serie.map((r) => ({ x: r.horodatage, retenu: r.retenu ? r.prix_trouves : 0, ecarte: r.retenu ? 0 : r.prix_trouves }));
    return h(
      "div",
      { class: "graphe-conteneur" },
      barres(donnees, {
        hauteur: 130,
        libelle: "Prix trouvés à chaque relevé de structure",
        formatX: (x) => UI.date(x, false).slice(0, 5),
        series: [
          { cle: "retenu", libelle: "Relevé compté (espacé d'une semaine)", couleur: COULEURS.rose },
          { cle: "ecarte", libelle: "Relevé trop rapproché, non compté", couleur: COULEURS.lilas },
        ],
      }),
      legende([
        { libelle: "Compté pour le critère 3", couleur: COULEURS.rose },
        { libelle: "Trop rapproché, non compté", couleur: COULEURS.lilas },
      ])
    );
  }

  window.Graphiques = {
    COULEURS: COULEURS,
    COULEURS_SCORE: COULEURS_SCORE,
    anneau: anneau,
    barres: barres,
    empilee: empilee,
    entonnoir: entonnoir,
    legende: legende,
    legendeScore: legendeScore,
    releves: releves,
    repartition: repartition,
  };
})();
