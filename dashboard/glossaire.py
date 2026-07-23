"""Centralised glossary powering the dashboard's interactive tooltips.

``GLOSSAIRE`` maps a term (matched exactly as it appears in a widget label or
in the generated ✓/✗/• explanation text) to a short, plain-language
explanation aimed at someone with no finance background. Two ways to use it:

  * Native Streamlit ``help=`` on widgets that support it (st.metric,
    st.selectbox, ...): pass ``help=GLOSSAIRE["Score technique"]`` directly.
  * Free text (the ✓/✗/• explanation, the LLM-written summary): call
    ``highlight_terms(text)`` to get an HTML string with every recognised
    term wrapped in a dotted-underline span whose ``title`` attribute shows
    the explanation on hover, then render it with
    ``st.markdown(html, unsafe_allow_html=True)``.

Keys intentionally match the codebase's existing accent-free French spelling
(e.g. "Fondamental reel", not "Fondamental réel") so they line up exactly
with the substrings produced by reasoning/opportunity_scoring.py's
build_explanation() -- the explanation VALUES are written with normal French
accents since they only ever reach a browser (HTML title attribute), never a
Windows console.
"""

import html
import re

GLOSSAIRE = {
    "RSI": (
        "Le RSI (Relative Strength Index) indique, sur une echelle de 0 a "
        "100, si une action a ete recemment beaucoup achetee ou beaucoup "
        "vendue. Au-dessus de 70 elle est jugee « surachetee » "
        "(elle a beaucoup monte, un retournement est possible) ; en dessous "
        "de 30 elle est « survendue »."
    ),
    "Momentum technique": (
        "Combine le RSI et la tendance des moyennes mobiles pour juger si "
        "le prix est plutot en dynamique haussiere ou baissiere en ce "
        "moment. Ce n'est pas un jugement sur la qualite de l'entreprise, "
        "seulement sur l'allure recente du graphique."
    ),
    "Moyenne mobile": (
        "Moyenne du prix de cloture sur une periode donnee (ex: 50 ou 200 "
        "jours de bourse), qui lisse les variations quotidiennes pour "
        "montrer la tendance de fond. Exemple : si le prix est au-dessus de "
        "sa moyenne 200 jours, la tendance de long terme est jugee "
        "haussiere."
    ),
    "MA 50": "Moyenne du prix de cloture sur les 50 derniers jours de bourse (environ 2-3 mois) -- une tendance de moyen terme.",
    "MA 200": "Moyenne du prix de cloture sur les 200 derniers jours de bourse (environ 10 mois) -- une tendance de long terme.",
    "Score technique": (
        "Note sur 100 qui resume la dynamique recente du prix (RSI + "
        "tendance des moyennes mobiles). Un score eleve veut dire que "
        "l'allure du graphique est plutot favorable en ce moment."
    ),
    "Technical": (
        "Note sur 100 qui resume la dynamique recente du prix (RSI + "
        "tendance des moyennes mobiles). Un score eleve veut dire que "
        "l'allure du graphique est plutot favorable en ce moment."
    ),
    "Prix/Valorisation": (
        "Note sur 100 qui juge si l'action semble chere ou bon marche par "
        "rapport a ses propres reperes (valorisation, tendance de prix, "
        "volatilite). Un score eleve suggere un prix d'entree plutot "
        "attractif."
    ),
    "Fondamental reel": (
        "Note sur 100 basee sur la vraie sante financiere de l'entreprise : "
        "croissance du chiffre d'affaires, marge beneficiaire, niveau de "
        "dette et evolution du cash-flow (tresorerie disponible). "
        "Contrairement aux scores prix/technique, il ne regarde pas le "
        "cours de bourse."
    ),
    "News recentes": (
        "Reflete le ton (positif/negatif) des actualites recentes de "
        "l'entreprise, analysees automatiquement. Absent si aucune "
        "actualite recente n'est disponible, plutot qu'un faux « "
        "neutre »."
    ),
    "Score global": (
        "Moyenne ponderee des scores independants disponibles pour ce "
        "ticker (prix/valorisation, technique, news, fondamental reel). "
        "C'est la note de synthese affichee en premier."
    ),
    "Score ajuste": (
        "Le score global multiplie par le niveau de confiance "
        "(score_global x confiance / 100). Il penalise un score eleve qui "
        "ne reposerait que sur peu de donnees fiables, pour classer les "
        "signaux du jour plus honnetement que le score brut seul."
    ),
    "Confiance": (
        "Indique combien des signaux independants (prix/valorisation, "
        "technique, news, fondamental reel) sont reellement disponibles et "
        "a jour pour ce ticker. 100% = tous presents et fiables ; un "
        "chiffre plus bas signale des donnees manquantes ou perimees."
    ),
    "Confidence": (
        "Indique combien des signaux independants sont reellement "
        "disponibles et fiables pour cette action. 100% = tout est present "
        "et a jour ; un chiffre plus bas signale des donnees manquantes."
    ),
    "Volatilite": (
        "Mesure l'ampleur des variations de prix d'une action sur une "
        "periode donnee, annualisee. Une volatilite elevee (ex: >40%) veut "
        "dire que le prix peut bouger fortement dans un sens comme dans "
        "l'autre -- plus de risque, dans les deux directions."
    ),
    "Volatility": (
        "Mesure l'ampleur des variations de prix d'une action, annualisee. "
        "Une volatilite elevee veut dire que le prix peut bouger fortement "
        "dans un sens comme dans l'autre."
    ),
    "Risque": (
        "Niveau de risque estime (Faible / Modere / Eleve), calcule a "
        "partir de la volatilite, du niveau de confiance des donnees, et "
        "d'eventuelles contradictions entre les signaux structurels "
        "(prix/valorisation, technique, fondamental reel)."
    ),
    "Breakout": (
        "Moment ou le prix d'une action sort clairement d'une zone ou il "
        "stagnait, en franchissant un niveau de resistance (a la hausse) ou "
        "de support (a la baisse) -- souvent avec un volume d'echange plus "
        "eleve que d'habitude."
    ),
    "Priorite": (
        "Niveau de suivi attribue a chaque ticker dans l'univers de "
        "l'application : « haute » (grandes valeurs suivies en "
        "priorite, couverture la plus complete), « moyenne » ou "
        "« basse » (couverture plus legere). Ce n'est pas une "
        "recommandation d'achat."
    ),
    "Volume": (
        "Nombre d'actions echangees sur une journee de bourse. Un volume "
        "inhabituellement eleve accompagne souvent un mouvement de prix "
        "important ou une actualite marquante."
    ),
    "Final score": (
        "Note globale combinant prix/valorisation, technique, volatilite et "
        "volume (page historique des valeurs suivies depuis le debut du "
        "projet)."
    ),
    "Entreprises a surveiller": (
        "Autres entreprises liees a ce ticker (concurrents, fournisseurs, "
        "clients, partenaires), identifiees dans le graphe de connaissances "
        "du projet -- utile pour comprendre le contexte concurrentiel d'un "
        "signal."
    ),
}


def term_span(display_text, key):
    """HTML span for a single labelled term (e.g. a section heading): shows
    GLOSSAIRE[key] as a native browser tooltip on hover. For a whole
    sentence/paragraph with several terms embedded in it, use
    highlight_terms() instead."""
    tip = html.escape(GLOSSAIRE.get(key, ""), quote=True)
    return (
        f'<span style="border-bottom:1px dotted #6b7280;cursor:help;" '
        f'title="{tip}">{html.escape(display_text)}</span>'
    )


_TERMS_BY_LENGTH = sorted(GLOSSAIRE, key=len, reverse=True)
_TERM_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TERMS_BY_LENGTH) + r")\b",
    re.IGNORECASE,
)
_KEY_BY_LOWER = {k.lower(): k for k in GLOSSAIRE}


def highlight_terms(text):
    """Wrap every recognised glossary term found in `text` with a
    dotted-underline span whose `title` attribute shows the plain-language
    explanation on hover. Escapes the source text first -- it may contain
    LLM-generated or externally-scraped content, never safe to inject as raw
    HTML. Render the result with st.markdown(..., unsafe_allow_html=True)."""
    escaped = html.escape(text or "")

    def _wrap(match):
        matched = match.group(0)
        key = _KEY_BY_LOWER.get(matched.lower())
        if key is None:
            return matched
        tip = html.escape(GLOSSAIRE[key], quote=True)
        return (
            f'<span style="border-bottom:1px dotted #6b7280;cursor:help;" '
            f'title="{tip}">{matched}</span>'
        )

    return _TERM_PATTERN.sub(_wrap, escaped)
