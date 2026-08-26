#!/usr/bin/env python3
"""Direction probability -- a qualitative hausse/stagnation/baisse split.

Combines signals already computed elsewhere in this project into three
percentages (summing to exactly 100) meant to answer "which way does the
available evidence lean, and how strongly": hausse / stagnation / baisse.

THIS IS NOT A STATISTICAL FORECAST. It is a transparent, rule-based
re-expression of scores this project already trusts (technical momentum,
price/valuation, causal-chain verdicts, structural coherence) as three
percentages instead of four separate 0-100 scores -- easier to read at a
glance, not a new source of predictive power. See DISCLAIMER below; every
caller that displays these percentages must show it alongside them.

Formula (fully traceable -- no black box)
-------------------------------------------
1. Each available input becomes a "lean" in [-1, +1] (negative = bearish,
   positive = bullish, 0 = neutral):
     - technique: (score_technique - 50) / 50
         Reuses reasoning/daily_summary.py's own score_technique, which is
         ALREADY the project's RSI + moving-average-position read (see
         analysis/combined_score.py) -- this module does not recompute RSI
         itself, it re-expresses a score this project already trusts.
     - valorisation: (score_prix_valorisation - 50) / 50
         Same rescaling for the price/valuation pillar.
     - causal: +1 (positif) / -1 (negatif) / 0 (neutre or absent), scaled
         by the chain's own confiance (0-100) -- see
         load_causal_effect_for_ticker() below for exactly where this
         value comes from (a STRUCTURED field already produced by
         reasoning/causal_reasoning.py, never re-derived by prompting an
         LLM a second time).

2. A weighted average of whichever leans are actually available (missing
   inputs are dropped, not treated as 0 -- their weight is proportionally
   redistributed across the inputs that ARE available, so a ticker with no
   causal chain is not artificially pulled toward stagnation just because
   one weight went unused):

     raw_lean = sum(lean_i * weight_i) / sum(weight_i actually used)

   Base weights: technique 0.45, valorisation 0.35, causal 0.20.

3. Coherence dampening: if reasoning/daily_summary.py's own has_conflict()
   flags a clear contradiction between price/valuation, technique and
   fondamental reel, raw_lean is multiplied by COHERENCE_DAMPING (0.5) --
   a signal built on components that disagree with each other is
   inherently less directional, whichever way it leans.

4. The dampened lean is split into three percentages:
     conviction = abs(lean)                                   # 0..1
     stagnation_pct = STAGNATION_MIN + (STAGNATION_MAX - STAGNATION_MIN) * (1 - conviction)
     directional_pool = 100 - stagnation_pct
     # split directional_pool between hausse/baisse according to lean's
     # sign and magnitude (lean=0 -> even split; lean=+-1 -> all one side)
   STAGNATION_MIN=15 / STAGNATION_MAX=55: stagnation never drops below 15%
   (this stays a qualitative estimate, never false certainty) and rises up
   to 55% when the evidence is weak or contradictory.
5. Rounded to whole percentages via largest-remainder rounding so the three
   values always sum to exactly 100.

Usable for ANY ticker that has at least one of score_technique /
score_prix_valorisation (i.e. anything reasoning/daily_summary.py's
build_signal() or load_ticker_detail() can produce) -- a ticker with
neither returns None, never a fabricated 33/33/34 split.

Optional news context (`news_tonalite`/`news_importance`)
-----------------------------------------------------------
Every caller above computes this from the ticker's GENERAL state only
(technical/valuation scores + any pre-existing causal chain) -- on the News
page specifically, that meant the percentages shown right below a fresh,
possibly strongly negative news item never reflected that news at all
(reasoning/analyze_news.py's get_or_generate_news_narrative was the one
caller with a specific news item in hand and wasn't passing it in), which
could silently contradict the narrative text sitting right above it.

Passing `news_tonalite`/`news_importance` (this ONE news item's own
already-computed fields, never re-derived) adds a fifth "news" lean
component to the SAME weighted average as everything else, scaled by the
news's own importance (1-10). Because it participates in the identical
weight-redistribution mechanism as every other component (see step 2
below), every EXISTING caller that never passes it is completely
unaffected -- this is additive, not a formula change for anyone else.

`horizon` in the result also changes when a news component is used: a
single news item's sentiment is a much shorter-lived effect than the
technical/valuation/causal read, so showing the SAME horizon label for
both would itself look like a silent inconsistency (a reader would not
know a "hausse 70%" and a "baisse 60%" a few days later were never
supposed to be compared against each other). See HORIZON_BASE/
HORIZON_NEWS below.
"""

import json

DISCLAIMER = (
    "Estimation qualitative basee sur les signaux disponibles (momentum "
    "technique, valorisation, coherence entre composantes, raisonnement "
    "causal si disponible) -- ce n'est PAS une prediction statistique "
    "validee ni une garantie de mouvement futur."
)

# --- Weights & bands (see module docstring for the full formula) -----------

WEIGHT_TECHNIQUE = 0.45
WEIGHT_VALORISATION = 0.35
WEIGHT_CAUSAL = 0.20
# Only ever added when a caller passes news_tonalite (the News page) --
# every other caller's total_weight (and therefore the exact ratio between
# technique/valorisation/causal) is completely unchanged, since weights are
# always renormalised over whichever components are actually present (see
# compute_direction_probabilities' total_weight below).
WEIGHT_NEWS = 0.20

COHERENCE_DAMPING = 0.5

STAGNATION_MIN = 15.0
STAGNATION_MAX = 55.0

# See module docstring's "Optional news context" section.
HORIZON_BASE = "sur les 5 a 10 prochains jours de bourse"
HORIZON_NEWS = "sur les tout prochains jours de bourse (effet immediat de cette actualite)"


def _lean(score):
    """0-100 score -> [-1, +1] lean, or None if the score itself is None."""
    if score is None:
        return None
    return (score - 50.0) / 50.0


def _news_lean(news_tonalite, news_importance):
    """Lean in [-1, 1] from ONE news item's own tonalite + importance, or
    None if `news_tonalite` isn't a recognised value (including None --
    "no news context passed" must stay indistinguishable from "absent",
    never silently coerced to neutral). Unlike causal_effect's "neutre"
    (which contributes no component at all, see caller below), a neutral
    NEWS item DOES contribute a real lean of exactly 0.0 -- tonalite is
    always populated for any analysed news (never null), so treating
    "neutre" as absent would make an important-but-neutral news
    indistinguishable from no news context at all, when it should
    genuinely pull the estimate toward stagnation (see module docstring)."""
    key = (news_tonalite or "").strip().lower()
    if key not in ("positive", "negative", "neutre"):
        return None
    if key == "neutre":
        return 0.0
    base = 1.0 if key == "positive" else -1.0
    scale = (news_importance / 10.0) if news_importance is not None else 1.0
    return base * scale


def load_causal_effect_for_ticker(conn, ticker):
    """Most recent causal-chain verdict on `ticker` AS AN IMPACTED COMPANY,
    or None. Deliberately does NOT look at chains where `ticker` is the
    chain's own ticker_source: causal_chains.entreprises_impactees already
    carries a STRUCTURED effet field (positif/negatif/neutre) for each
    company a chain names as impacted (see reasoning/causal_reasoning.py),
    but the chain's own source company has no equivalent structured
    self-verdict -- only free-form prose. Reusing the structured field for
    impacted companies keeps this fully traceable (a real value another
    module already computed); guessing a source company's own direction
    from its prose would not be.

    Scans the most recent chains (bounded, not the whole table -- causal
    chains accumulate slowly, capped by CAUSAL_REASONING_DAILY_LIMIT=5/day)
    for the first (i.e. most recent) one naming this ticker."""
    rows = conn.execute(
        "SELECT entreprises_impactees, confiance, created_at FROM causal_chains "
        "WHERE entreprises_impactees IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    for impactees_json, confiance, created_at in rows:
        try:
            impactees = json.loads(impactees_json) if impactees_json else []
        except (ValueError, TypeError):
            continue
        for entry in impactees:
            if not isinstance(entry, dict):
                continue
            if (entry.get("ticker") or "").strip().upper() == ticker.upper():
                effet = str(entry.get("effet") or "").strip().lower()
                if effet in ("positif", "negatif", "neutre"):
                    return {
                        "effet": effet,
                        "confiance": confiance,
                        "date": (created_at or "")[:10],
                        "entreprise": entry.get("entreprise"),
                    }
    return None


def _largest_remainder_round(values):
    """Round a dict of {label: float percentage} to whole numbers that
    still sum to exactly 100 -- naive round() on each value independently
    can drift the total to 99 or 101 (e.g. 33.4/33.3/33.3 -> 33/33/33=99
    isn't the failure case, but 33.5/33.5/33.0 -> 34/34/33=101 is), which
    would silently break any caller assuming the three percentages sum to
    100. Standard largest-remainder method: floor everything, then hand
    the leftover points to whichever values had the largest fractional
    part, largest first."""
    floors = {k: int(v) for k, v in values.items()}
    remainder = 100 - sum(floors.values())
    fractions = sorted(values.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)
    for i in range(remainder):
        key = fractions[i % len(fractions)][0]
        floors[key] += 1
    return floors


def compute_direction_probabilities(
    score_technique=None,
    score_prix_valorisation=None,
    score_fondamental_reel=None,
    causal_effect=None,
    causal_confidence=None,
    news_tonalite=None,
    news_importance=None,
):
    """{"hausse": int, "stagnation": int, "baisse": int, "horizon": str,
    "explication": str, "disclaimer": str} summing hausse+stagnation+baisse
    to exactly 100, or None if neither score_technique nor
    score_prix_valorisation is available (nothing to compute from at all).

    `score_fondamental_reel` is used ONLY for has_conflict()'s coherence
    check (see module docstring step 3) -- it does not contribute a lean
    of its own, since it is already one of the three components
    has_conflict() cross-checks the other two against.

    `causal_effect` is "positif" | "negatif" | "neutre" | None (from
    load_causal_effect_for_ticker() above, or any other structured source
    a caller already has); `causal_confidence` is that verdict's own 0-100
    confiance, used to scale the causal lean's strength.

    `news_tonalite`/`news_importance` -- see the module docstring's
    "Optional news context" section -- are ONE specific news item's own
    already-computed fields (reasoning/analyze_news.py's news_analysis
    table), passed only by the News page's narrative generation. Every
    other caller leaves these None and is completely unaffected.

    `horizon` is HORIZON_NEWS when a news component was actually used,
    HORIZON_BASE otherwise -- always show it next to the percentages so a
    reader never mistakes a short news-driven read for the longer
    technical/valuation one, or vice versa.

    `explication` is a French, human-readable trace of every component
    that went into the result -- built so a caller can show WHY the split
    came out this way, never a black box."""
    # Local import: has_conflict lives in daily_summary.py, and importing
    # it at module load time would make this module (usable standalone,
    # e.g. from a lightweight script) drag in daily_summary's own Groq/
    # groq_config imports. Deferred to call time instead.
    from reasoning.daily_summary import has_conflict

    components = []  # (label, lean, weight) for every AVAILABLE input
    trace = []

    tech_lean = _lean(score_technique)
    if tech_lean is not None:
        components.append(("technique", tech_lean, WEIGHT_TECHNIQUE))
        trace.append(
            f"Momentum technique (score {score_technique:.0f}/100) -> "
            f"inclinaison {tech_lean:+.2f}."
        )

    val_lean = _lean(score_prix_valorisation)
    if val_lean is not None:
        components.append(("valorisation", val_lean, WEIGHT_VALORISATION))
        trace.append(
            f"Prix/valorisation (score {score_prix_valorisation:.0f}/100) -> "
            f"inclinaison {val_lean:+.2f}."
        )

    if causal_effect in ("positif", "negatif"):
        causal_lean = 1.0 if causal_effect == "positif" else -1.0
        conf_scale = (causal_confidence / 100.0) if causal_confidence is not None else 1.0
        causal_lean *= conf_scale
        components.append(("causal", causal_lean, WEIGHT_CAUSAL))
        trace.append(
            f"Raisonnement causal (effet {causal_effect}, confiance "
            f"{causal_confidence if causal_confidence is not None else 'n/a'}) -> "
            f"inclinaison {causal_lean:+.2f}."
        )
    elif causal_effect == "neutre":
        trace.append("Raisonnement causal disponible mais neutre -> aucune inclinaison ajoutee.")

    news_lean = _news_lean(news_tonalite, news_importance)
    news_used = news_lean is not None
    if news_used:
        components.append(("news", news_lean, WEIGHT_NEWS))
        trace.append(
            f"Cette news (tonalite {news_tonalite}, importance "
            f"{news_importance if news_importance is not None else 'n/a'}/10) -> "
            f"inclinaison {news_lean:+.2f}."
        )

    if not components:
        return None

    total_weight = sum(w for _, _, w in components)
    raw_lean = sum(lean * w for _, lean, w in components) / total_weight

    conflict = has_conflict(score_prix_valorisation, score_technique, score_fondamental_reel)
    if conflict:
        raw_lean *= COHERENCE_DAMPING
        trace.append(
            "Contradiction detectee entre composantes structurelles -> "
            f"inclinaison amortie (x{COHERENCE_DAMPING})."
        )

    conviction = min(1.0, abs(raw_lean))
    stagnation_pct = STAGNATION_MIN + (STAGNATION_MAX - STAGNATION_MIN) * (1 - conviction)
    directional_pool = 100.0 - stagnation_pct

    if raw_lean >= 0:
        hausse_pct = directional_pool * (0.5 + raw_lean / 2)
        baisse_pct = directional_pool - hausse_pct
    else:
        baisse_pct = directional_pool * (0.5 + (-raw_lean) / 2)
        hausse_pct = directional_pool - baisse_pct

    rounded = _largest_remainder_round(
        {"hausse": hausse_pct, "stagnation": stagnation_pct, "baisse": baisse_pct}
    )

    trace.append(
        f"Inclinaison globale ponderee : {raw_lean:+.2f} -> "
        f"hausse {rounded['hausse']}% / stagnation {rounded['stagnation']}% / "
        f"baisse {rounded['baisse']}%."
    )

    return {
        "hausse": rounded["hausse"],
        "stagnation": rounded["stagnation"],
        "baisse": rounded["baisse"],
        "horizon": HORIZON_NEWS if news_used else HORIZON_BASE,
        "explication": " ".join(trace),
        "disclaimer": DISCLAIMER,
    }
