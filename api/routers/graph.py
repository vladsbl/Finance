"""GET /api/graph/* -- NOT YET IMPLEMENTED.

Planned to wrap graph/build_graph.py (load_relations, build_graph,
direct_relations) for the Knowledge Graph page: nodes/edges for the
interactive graph view, plus the manual add/list/delete relation actions
already implemented in dashboard/app.py (add_manual_relation,
load_manual_relations, delete_manual_relation) -- those three are already
conn-first, dict-in/dict-out functions with no Streamlit dependency, so
they can be called from here unchanged.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/graph", tags=["graph"])
