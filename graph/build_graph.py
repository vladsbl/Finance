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
