"""GET/POST/DELETE /api/graph/* -- JSON view of dashboard/app.py's
"Knowledge Graph" page.

Every route here is a thin wrapper around graph/build_graph.py
(load_relations, build_graph, graph_to_json, add_manual_relation,
load_manual_relations, delete_manual_relation) and
reasoning/daily_summary.py (load_opportunites_multi,
resolve_data_dates_by_priority, load_all_tickers_with_names) -- no graph
construction, dedup, or scoring logic is reimplemented here.
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from api.dependencies import get_db, normalise_ticker
from graph.build_graph import (
    RELATION_TYPES,
    add_manual_relation,
    build_graph,
    delete_manual_relation,
    graph_to_json,
    load_manual_relations,
    load_relations,
)
from reasoning.daily_summary import (
    load_all_tickers_with_names,
    load_opportunites_multi,
    resolve_data_dates_by_priority,
)

router = APIRouter(prefix="/api/graph", tags=["graph"])


def _top_opportunity_tickers(conn, limit=10):
    """Today's best `limit` tickers by score_global, each priorite tier
    resolved to its OWN latest date_calcul -- same source and same sort as
    dashboard/app.py's OPPORTUNITES_SQL (`ORDER BY (score_global IS NULL),
    score_global DESC`) and api/routers/opportunities.py, just capped to
    the top N instead of paginated, since this is only used to decide
    which nodes the graph highlights as "primary"."""
    dates_by_priority = resolve_data_dates_by_priority(conn)
    rows = load_opportunites_multi(conn, dates_by_priority)
    rows.sort(key=lambda r: (r["score_global"] is None, -(r["score_global"] or 0)))
    return [r["ticker"] for r in rows[:limit]]


@router.get("")
def get_graph(ticker: str | None = None, conn=Depends(get_db)):
    """Default: the full Knowledge Graph, with today's top-10 opportunites
    highlighted as "primary" nodes -- same as render_graph_page()'s default
    view (build_graph(relations, tracked=set(top_tickers))).

    ?ticker=XXX: centered on ONE ticker's direct relations only (source ==
    ticker), matching graph/build_graph.py's direct_relations() scope --
    the same "relations directes" the Streamlit page's ticker picker shows
    as text, here as a small subgraph instead. 404 if the ticker isn't in
    `universe`; an empty {nodes: [], edges: []} (not a 404) if it exists
    but has no relations at all yet."""
    relations = load_relations(conn)
    names = load_all_tickers_with_names(conn)

    if ticker is not None:
        ticker = normalise_ticker(ticker)
        if ticker not in names:
            raise HTTPException(
                status_code=404,
                detail=f"{ticker} n'est pas dans l'univers suivi.",
            )
        direct = [r for r in relations if r["source_ticker"].strip() == ticker]
        graph = build_graph(direct, tracked={ticker})
        payload = {"mode": "ticker", "ticker": ticker}
    else:
        top_tickers = _top_opportunity_tickers(conn)
        graph = build_graph(relations, tracked=set(top_tickers))
        payload = {"mode": "top_opportunities", "top_tickers": top_tickers}

    body = graph_to_json(graph, names)
    n_primary = sum(1 for n in body["nodes"] if n["kind"] == "primary")
    payload.update(body)
    payload["n_primary"] = n_primary
    payload["n_external"] = len(body["nodes"]) - n_primary
    return payload


@router.get("/relations/manual")
def get_manual_relations(conn=Depends(get_db)):
    """Relations added via the manual form/route (origine='manuel'), for
    the admin list/delete panel -- also returns the valid relation_type
    values (RELATION_TYPES) so the frontend's add-relation form never
    hardcodes its own copy of that list."""
    return {
        "relations": load_manual_relations(conn),
        "relation_types": RELATION_TYPES,
    }


@router.post("/relations", status_code=201)
def post_manual_relation(
    source_ticker: str = Body(...),
    relation_type: str = Body(...),
    target_name: str = Body(...),
    target_ticker: str | None = Body(None),
    notes: str | None = Body(None),
    conn=Depends(get_db),
):
    """Add one manually-curated relation -- same validations as
    dashboard/app.py's "Ajouter une relation manuellement" form: relation_type
    must be one of RELATION_TYPES, target_name is required, a target_ticker
    (when given) must differ from the source, and add_manual_relation()
    itself dedups on (source, type, target) before inserting. 201 with the
    newly created relation on success; 409 if it's a duplicate; 422 for any
    other invalid input; 404 if source_ticker isn't in `universe` (this
    route, unlike the dashboard's selectbox, accepts free-text source, so
    that check has to happen here instead of being implicit)."""
    source_ticker = normalise_ticker(source_ticker)
    relation_type = (relation_type or "").strip()
    target_name = (target_name or "").strip()
    target_ticker = (target_ticker or "").strip().upper() or None
    notes = (notes or "").strip() or None

    if relation_type not in RELATION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"relation_type invalide : {relation_type!r} (attendu : {RELATION_TYPES})",
        )
    if not target_name:
        raise HTTPException(status_code=422, detail="Le nom de l'entreprise cible est obligatoire.")
    if target_ticker == source_ticker:
        raise HTTPException(
            status_code=422,
            detail="Le ticker cible doit etre different du ticker source.",
        )

    names = load_all_tickers_with_names(conn)
    if source_ticker not in names:
        raise HTTPException(
            status_code=404,
            detail=f"{source_ticker} n'est pas dans l'univers suivi.",
        )

    ok, error = add_manual_relation(
        conn, source_ticker, relation_type, target_name, target_ticker, notes,
    )
    if not ok:
        raise HTTPException(status_code=409, detail=error)

    return load_manual_relations(conn)[0]  # freshest row (ORDER BY id DESC) is the one just inserted


@router.delete("/relations/{relation_id}")
def delete_relation(relation_id: int, conn=Depends(get_db)):
    """Delete one manually-added relation -- delete_manual_relation() itself
    is scoped to origine='manuel', so this can never remove an
    auto-generated/pilot-seed relation regardless of the id passed in. 404
    if the id doesn't exist or isn't a manual relation."""
    deleted = delete_manual_relation(conn, relation_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Relation manuelle {relation_id} introuvable (deja supprimee ?).",
        )
    return {"deleted": True, "id": relation_id}
