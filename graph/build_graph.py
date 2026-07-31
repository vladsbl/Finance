#!/usr/bin/env python3
"""Build a knowledge graph of ticker relations with networkx.

Reads the ``relations`` table (populated by graph/import_relations.py) and
builds a directed graph where:
  * the 10 tracked tickers are primary nodes,
  * external entities (untracked or without a ticker) are secondary nodes,
  * each edge is labelled by its relation_type (concurrent / fournisseur /
    client / partenaire / ...).

Run directly for a quick summary:
    python graph/build_graph.py
"""

import logging
import os
import sqlite3
import sys

import networkx as nx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

from graph.import_relations import CREATE_TABLE_SQL as CREATE_RELATIONS_TABLE_SQL  # noqa: E402
from ingestion.fetch_prices import SYMBOLS  # noqa: E402

TRACKED = set(SYMBOLS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build_graph")

LOAD_RELATIONS_SQL = """
SELECT source_ticker, relation_type, target_name, target_ticker, notes
FROM relations
ORDER BY source_ticker, relation_type, target_name;
"""


def load_relations(conn):
    """Return relations as a list of dicts."""
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(LOAD_RELATIONS_SQL)]


def _target_node_id(rel):
    """Node id for a relation target: its ticker if any, else its name."""
    ticker = (rel.get("target_ticker") or "").strip()
    return ticker if ticker else rel["target_name"]


def build_graph(relations, tracked=TRACKED):
    """Build a directed knowledge graph from relation dicts.

    Nodes carry ``kind`` ("primary"/"external"), ``label`` and ``ticker``.
    Edges carry ``relation`` (the relation_type) and ``notes``.
    """
    graph = nx.DiGraph()

    def add_node(node_id, label, ticker):
        kind = "primary" if ticker and ticker in tracked else "external"
        if graph.has_node(node_id):
            # Upgrade to primary if we learn it is a tracked ticker.
            if kind == "primary":
                graph.nodes[node_id]["kind"] = "primary"
            return
        graph.add_node(node_id, label=label, ticker=ticker or "", kind=kind)

    for rel in relations:
        src = rel["source_ticker"].strip()
        add_node(src, src, src)  # sources are always tracked tickers

        tgt_id = _target_node_id(rel)
        tgt_ticker = (rel.get("target_ticker") or "").strip()
        add_node(tgt_id, rel["target_name"], tgt_ticker)

        graph.add_edge(src, tgt_id,
                       relation=rel["relation_type"],
                       notes=rel.get("notes") or "")
    return graph


def direct_relations(relations, ticker):
    """Group a ticker's outbound relations by type.

    Returns a dict {relation_type: [display_name, ...]} where display_name is
    the target name plus its ticker in parentheses when different.
    """
    grouped = {}
    for rel in relations:
        if rel["source_ticker"].strip() != ticker:
            continue
        name = rel["target_name"]
        tk = (rel.get("target_ticker") or "").strip()
        display = f"{name} ({tk})" if tk and tk != name else name
        grouped.setdefault(rel["relation_type"], []).append(display)
    return grouped


def summary_line(relations, ticker):
    """One-line human summary, e.g. 'NVDA : concurrent de AMD (AMD), ...'."""
    grouped = direct_relations(relations, ticker)
    if not grouped:
        return f"{ticker} : aucune relation connue."
    parts = [f"{rtype} {', '.join(names)}" for rtype, names in grouped.items()]
    return f"{ticker} : " + " ; ".join(parts)


def graph_to_json(graph, names):
    """Serialize a build_graph() networkx graph to plain JSON-native
    nodes/edges -- same node/edge data dashboard/app.py's pyvis rendering
    (_graph_html) uses (kind, label, ticker, relation, notes), just as
    dicts instead of an HTML/JS graph. `names` is {ticker: display_name}
    (e.g. reasoning.daily_summary.load_all_tickers_with_names) -- primary
    nodes get their real company name as `display_name` when known,
    external nodes just reuse their own label (already a real name, coming
    straight from `relations.target_name`)."""
    nodes = [
        {
            "id": node_id,
            "kind": d["kind"],
            "ticker": d["ticker"],
            "label": d["label"],
            "display_name": names.get(node_id, node_id) if d["kind"] == "primary" else d["label"],
        }
        for node_id, d in graph.nodes(data=True)
    ]
    edges = [
        {"source": u, "target": v, "relation_type": d["relation"], "notes": d.get("notes") or ""}
        for u, v, d in graph.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


# --- Manual relations (human-added, origine='manuel') -----------------------
#
# Relocated from dashboard/app.py's Knowledge Graph page: these five were
# already conn-first, dict-in/dict-out functions with no Streamlit
# dependency, so this is a straight move (no rewrite) -- dashboard/app.py
# now imports them from here instead of defining them locally, so the API
# and the Streamlit page share the exact same add/list/delete logic.

RELATION_TYPES = ["concurrent", "fournisseur", "client", "partenaire", "dependance"]

RELATION_TYPE_HELP = (
    "Sens de la relation, du point de vue du ticker SOURCE (convention deja "
    "en usage dans tout le Knowledge Graph -- verifiee sur AAPL/TSMC et "
    "NVDA/clients) :\n"
    "- concurrent : concurrence directe (relation symetrique)\n"
    "- fournisseur : la CIBLE fournit la SOURCE (ex: source=AAPL, "
    "fournisseur, cible=TSM -- TSMC fournit Apple)\n"
    "- client : la CIBLE est cliente de la SOURCE (la source fournit la cible)\n"
    "- partenaire : partenariat (relation symetrique)\n"
    "- dependance : la source depend d'une matiere premiere/d'un facteur "
    "externe (la cible peut ne pas avoir de ticker reel)"
)


def _ensure_relations_origine_column(conn):
    """Backfills `origine` onto a `relations` table created before this
    column existed -- import_relations.py's own CREATE_TABLE_SQL doesn't
    include it. Every pre-existing row (hand-curated pilot seed CSV +
    Groq-generated batches activated after human review) is tagged 'auto',
    so only relations added through the manual form/route are ever
    'manuel' -- the distinction future audits need to tell the two apart."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(relations)")}
    if "origine" in cols:
        return
    conn.execute("ALTER TABLE relations ADD COLUMN origine TEXT NOT NULL DEFAULT 'auto'")
    conn.commit()


def _relation_duplicate(conn, source_ticker, relation_type, target_ticker, target_name):
    """Same empirical dedup rule already used when activating a reviewed
    Groq batch into `relations`: a target with a real ticker is matched on
    (source, type, ticker) -- never on the raw name text, which can vary in
    wording for the same real company (e.g. "TSMC" vs "Taiwan Semiconductor
    Manufacturing Company") -- an unresolved/external target (no ticker) is
    matched on the exact (source, type, name) tuple instead."""
    if target_ticker:
        row = conn.execute(
            "SELECT id FROM relations WHERE source_ticker=? AND relation_type=? "
            "AND target_ticker=?",
            (source_ticker, relation_type, target_ticker),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM relations WHERE source_ticker=? AND relation_type=? "
            "AND target_name=? AND (target_ticker IS NULL OR target_ticker = '')",
            (source_ticker, relation_type, target_name),
        ).fetchone()
    return row is not None


def add_manual_relation(conn, source_ticker, relation_type, target_name, target_ticker, notes):
    """Insert one manually-curated relation directly into the active
    Knowledge Graph (`relations`) -- no relations_generated/statut detour,
    since the human adding it here (dashboard form or API route) already IS
    the validation step. Returns (ok, error_message_or_None). Never raises."""
    conn.execute(CREATE_RELATIONS_TABLE_SQL)
    _ensure_relations_origine_column(conn)
    if _relation_duplicate(conn, source_ticker, relation_type, target_ticker, target_name):
        return False, "Cette relation existe deja dans le Knowledge Graph (meme source/type/cible)."
    try:
        conn.execute(
            "INSERT INTO relations (source_ticker, relation_type, target_name, "
            "target_ticker, notes, origine) VALUES (?, ?, ?, ?, ?, 'manuel')",
            (source_ticker, relation_type, target_name, target_ticker or None,
             notes or None),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False, "Cette relation existe deja (contrainte d'unicite)."
    return True, None


def load_manual_relations(conn):
    """All relations added via the manual form/route (origine='manuel'),
    freshest first -- deliberately not cached anywhere: both the dashboard
    admin panel and the API's list route must reflect an add/delete from
    earlier in the very same request/script run."""
    _ensure_relations_origine_column(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source_ticker, relation_type, target_name, target_ticker, notes "
        "FROM relations WHERE origine = 'manuel' ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_manual_relation(conn, relation_id):
    """Delete one relation by id -- scoped to origine='manuel' so this
    admin control can never remove an auto-generated/pilot-seed relation
    even if called with the wrong id."""
    cur = conn.execute(
        "DELETE FROM relations WHERE id = ? AND origine = 'manuel'", (relation_id,))
    conn.commit()
    return cur.rowcount > 0


def main():
    if not os.path.exists(DB_PATH):
        logger.error("Database not found. Run graph/import_relations.py first.")
        return 1
    try:
        conn = sqlite3.connect(DB_PATH)
        relations = load_relations(conn)
        conn.close()
    except sqlite3.Error as exc:
        logger.error("Could not read relations: %s", exc)
        return 1

    if not relations:
        logger.warning("No relations found. Run graph/import_relations.py first.")
        return 1

    graph = build_graph(relations)
    primary = [n for n, d in graph.nodes(data=True) if d["kind"] == "primary"]
    external = [n for n, d in graph.nodes(data=True) if d["kind"] == "external"]
    logger.info("Graph: %d nodes (%d primary, %d external), %d edges.",
                graph.number_of_nodes(), len(primary), len(external),
                graph.number_of_edges())
    for ticker in SYMBOLS:
        logger.info("%s", summary_line(relations, ticker))
    return 0


if __name__ == "__main__":
    sys.exit(main())
